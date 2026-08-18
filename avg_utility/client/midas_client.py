"""Midas redemption-queue reader — mTokens sitting in an issuer redemption vault.

Midas standard ("async") redemption does NOT burn on request. `redeemRequest` transfers the
mToken into the redemption vault, where it sits unburned until an operator approves it. During
that window the holding is invisible to every balance-based tracker:

    mToken.balanceOf(ourWallet)        == 0     <- what a balance scraper sees
    mToken.balanceOf(redemptionVault)  == ours  <- commingled with everyone else's
    no ERC20 anywhere represents the claim

So the position has to be reconstructed from the vault's request book. Two rules, both learned
from reading `RedemptionVault._redeemRequest` on mainnet:

1. VALUE THE STRUCT, NEVER THE EVENT. The `RedeemRequest` event carries `amountMTokenIn`, the
   GROSS amount. The contract sends `feeAmount` straight to `feeReceiver` at request time and
   escrows only `amountMTokenWithoutFee`, which is what `redeemRequests(id).amountMToken` holds.
   The two are identical today only because the standard-redemption fee is 0. Reading the struct
   stays correct the day that changes; reading the event would silently over-value.

2. `Request.sender` IS THE RECIPIENT, NOT THE CALLER. The contract stores `sender: recipient`, so
   for `redeemRequest(tokenOut, amount, recipient)` the field holds whoever gets paid. Filtering
   on it therefore attributes the claim to the payee, which is the right economic owner — a
   request someone else filed on our behalf counts, and one we filed that pays elsewhere does not.

The claim is denominated in mToken, not in the payout token: settlement pays
`amountMToken * NAV_at_approval / tokenOutRate`, and the stored `mTokenRate` is the rate at
REQUEST time, which is precisely the number that turns out to be wrong. So this returns a native
mToken quantity for the caller to price at the current NAV — the same single-asset shape as a
Graph delegation, and the same price the wallet's liquid mToken balance uses.

Discovery is by on-chain enumeration at the target block (`currentRequestId` → `redeemRequests(i)`),
not by log scanning: it is deterministic, needs no indexer or API key, works on an archive node for
any historical block, and reports each request's status AS OF that block, so a valuation backfill
is reproducible.
"""
import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from web3 import Web3

logger = logging.getLogger(__name__)

_ABI_DIR = Path(__file__).resolve().parent.parent / "abi"
_VAULT_ABI = json.loads((_ABI_DIR / "MidasRedemptionVault.json").read_text())

# Midas RequestStatus enum (contracts/interfaces/IRedemptionVault.sol).
_STATUS = {0: "Pending", 1: "Processed", 2: "Canceled"}
_STATUS_PENDING = 0

# Runaway guard against a misconfigured address turning into an unbounded scan. Generous because
# the scan batches (see below): 50k requests is 100 RPC calls, not 50k.
_MAX_REQUESTS_SCAN = 50_000

# Multicall3 — same deterministic address on Ethereum, Base and Arbitrum. Reading the book one
# request at a time is a round trip each; batching turns N of them into ceil(N / _MULTICALL_BATCH),
# executed against a SINGLE block state, so block pinning is unaffected.
#
# Only used above the threshold. For a short book the batch is measurably SLOWER (one mainnet
# sample: 4 requests took 652 ms batched vs 430 ms as individual calls) because the encode/decode
# and the extra hop cost more than four reused-connection round trips. Real books today range from
# 4 (mWIN) to ~2,000 (mHYPER), so both paths matter.
_MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
_MULTICALL_THRESHOLD = 500
_MULTICALL_BATCH = 500

# Calldata is hand-built rather than via contract.encode_abi: this package supports web3 >=6, and
# the encode helper was renamed between 6.x (`encodeABI`) and 7.x (`encode_abi`).
_REQUEST_SELECTOR = Web3.keccak(text="redeemRequests(uint256)")[:4]
_REQUEST_TYPES = ["address", "address", "uint8", "uint256", "uint256", "uint256"]

_MULTICALL3_ABI = json.loads((_ABI_DIR / "Multicall3.json").read_text())

# mToken is always 18 decimals (asserted by the vault itself, which hardcodes 18 on transfer).
MTOKEN_DECIMALS = 18

# Midas DataFeed wraps a Chainlink-style aggregator; `getDataInBase18` is the price the vault
# itself uses. Read for HEALTH ONLY — the position's price comes from the underlying token's
# normal price source, so the queue and the liquid balance mark identically.
_DATA_FEED_ABI = [
    {"name": "getDataInBase18", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "uint256"}]},
    {"name": "aggregator", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "address"}]},
    {"name": "healthyDiff", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "uint256"}]},
]

_AGGREGATOR_ABI = [
    {"name": "decimals", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "uint8"}]},
    {"name": "latestRoundData", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"name": "roundId", "type": "uint80"}, {"name": "answer", "type": "int256"},
                 {"name": "startedAt", "type": "uint256"}, {"name": "updatedAt", "type": "uint256"},
                 {"name": "answeredInRound", "type": "uint80"}]},
]


def _block_kw(block: Optional[int]) -> dict:
    return {"block_identifier": block} if block is not None else {}


class MidasRedemptionClient:
    """On-chain reads against Midas redemption vaults."""

    def __init__(self):
        self._w3_cache: Dict[str, Web3] = {}

    def _get_web3(self, rpc_url: str) -> Web3:
        if rpc_url not in self._w3_cache:
            self._w3_cache[rpc_url] = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))
        return self._w3_cache[rpc_url]

    # ---- request-book reads ----

    def _read_request_book(
        self, w3: Web3, vault: str, count: int, kw: dict
    ) -> List[tuple]:
        """Every request struct in the vault's book, index-aligned to its request id.

        Batches through Multicall3 once the book is long enough to be worth it, and falls back to
        one call per request when it can't (short book, or no Multicall3 deployed at this block —
        which is the case for any block before its deployment).
        """
        if count < _MULTICALL_THRESHOLD:
            return self._read_request_book_individually(w3, vault, count, kw)

        multicall = Web3.to_checksum_address(_MULTICALL3)
        if not w3.eth.get_code(multicall, **kw):
            logger.warning(
                "Multicall3 not deployed at %s for this block — falling back to %d individual "
                "reads", multicall, count,
            )
            return self._read_request_book_individually(w3, vault, count, kw)

        contract = w3.eth.contract(address=multicall, abi=_MULTICALL3_ABI)
        target = Web3.to_checksum_address(vault)
        structs: List[tuple] = []
        for start in range(0, count, _MULTICALL_BATCH):
            ids = range(start, min(start + _MULTICALL_BATCH, count))
            # allowFailure=False: a read that reverts must take the batch down rather than come
            # back as an empty result that decodes to a zero-value (i.e. no pending claim).
            calls = [
                (target, False, _REQUEST_SELECTOR + request_id.to_bytes(32, "big"))
                for request_id in ids
            ]
            for _success, data in contract.functions.aggregate3(calls).call(**kw):
                structs.append(tuple(w3.codec.decode(_REQUEST_TYPES, data)))
        logger.debug(
            "Midas vault %s: read %d request(s) in %d batched call(s)",
            vault, count, (count + _MULTICALL_BATCH - 1) // _MULTICALL_BATCH,
        )
        return structs

    @staticmethod
    def _read_request_book_individually(
        w3: Web3, vault: str, count: int, kw: dict
    ) -> List[tuple]:
        contract = w3.eth.contract(address=Web3.to_checksum_address(vault), abi=_VAULT_ABI)
        return [contract.functions.redeemRequests(i).call(**kw) for i in range(count)]

    # ---- reads ----

    def get_pending_requests(
        self,
        vault: str,
        holders: Iterable[str],
        rpc_url: str,
        block: Optional[int] = None,
        mtoken: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Pending redemption requests payable to `holders` in one vault, at `block`.

        Raises on any read failure. A vault that cannot be read must NOT contribute 0 — that would
        silently under-value the position, which is the flattering direction.

        Args:
            vault: redemption vault address.
            holders: addresses whose claims to count (matched against `Request.sender`, the payee).
            rpc_url: RPC for the vault's chain (archive node required for historical blocks).
            block: block to pin every read to (None = latest).
            mtoken: expected mToken; when given, `vault.mToken()` must match or this raises.
                Catches the "pasted the wrong product's vault" config error.

        Returns:
            {vault, mtoken, current_request_id, scanned, pending_raw, pending_count, requests: [...]}
            where `pending_raw` is our claim in wei.
        """
        w3 = self._get_web3(rpc_url)
        vault_cs = Web3.to_checksum_address(vault)
        contract = w3.eth.contract(address=vault_cs, abi=_VAULT_ABI)
        kw = _block_kw(block)
        wanted = {h.lower() for h in holders}

        vault_mtoken = contract.functions.mToken().call(**kw)
        if mtoken and vault_mtoken.lower() != mtoken.lower():
            raise ValueError(
                f"Midas vault {vault_cs} redeems {vault_mtoken}, not the configured mToken {mtoken}"
            )

        current_request_id = contract.functions.currentRequestId().call(**kw)
        if current_request_id > _MAX_REQUESTS_SCAN:
            raise ValueError(
                f"Midas vault {vault_cs} has {current_request_id} requests, over the "
                f"{_MAX_REQUESTS_SCAN} scan guard — batch the reads before raising it"
            )

        requests: List[Dict[str, Any]] = []
        pending_raw = 0
        book = self._read_request_book(w3, vault_cs, current_request_id, kw)
        for request_id, struct in enumerate(book):
            sender, token_out, status, amount_mtoken, mtoken_rate, token_out_rate = struct
            if status != _STATUS_PENDING:
                continue
            if sender.lower() not in wanted:
                continue
            pending_raw += amount_mtoken
            requests.append({
                "vault": vault_cs,
                "request_id": request_id,
                # Stored as `sender` on-chain but holds the RECIPIENT — named for what it means.
                "recipient": sender,
                "token_out": token_out,
                "status": _STATUS.get(status, str(status)),
                # Already net of fee: the fee left for feeReceiver at request time.
                "amount_mtoken_raw": amount_mtoken,
                "amount_mtoken": amount_mtoken / 10 ** MTOKEN_DECIMALS,
                # Rate at REQUEST time. Recorded for audit; settlement uses NAV at approval, so
                # this must never be used to value the claim.
                "mtoken_rate_at_request": mtoken_rate / 10 ** MTOKEN_DECIMALS,
                "token_out_rate_at_request": token_out_rate / 10 ** MTOKEN_DECIMALS,
            })

        logger.info(
            "Midas vault %s: scanned %d request(s), %d pending for us = %.6f mToken",
            vault_cs, current_request_id, len(requests), pending_raw / 10 ** MTOKEN_DECIMALS,
        )

        return {
            "vault": vault_cs,
            "mtoken": vault_mtoken,
            "current_request_id": current_request_id,
            "scanned": current_request_id,
            "pending_raw": pending_raw,
            "pending_count": len(requests),
            "requests": requests,
        }

    def get_nav_feed_health(
        self,
        vault: str,
        rpc_url: str,
        block: Optional[int] = None,
    ) -> Dict[str, Any]:
        """The vault's NAV feed and whether it is answering — DIAGNOSTIC ONLY, never the price.

        The feed is derived from the vault (`mTokenDataFeed()`) rather than stored in config, so it
        cannot go stale when Midas repoints it. It matters because an unhealthy feed is exactly what
        strands a redemption: a sibling product's request sat pending 47+ days with its feed
        reverting `DF: feed is unhealthy`, so no NAV update could be posted and nothing could be
        approved. Never raises — a diagnostic must not take a valuation down.
        """
        out = {"feed": None, "aggregator": None, "nav_usd": None, "updated_at": None,
               "age_s": None, "healthy_diff_s": None, "stale": None, "error": None}
        try:
            w3 = self._get_web3(rpc_url)
            kw = _block_kw(block)
            vault_c = w3.eth.contract(address=Web3.to_checksum_address(vault), abi=_VAULT_ABI)
            feed_addr = vault_c.functions.mTokenDataFeed().call(**kw)
            out["feed"] = feed_addr

            feed = w3.eth.contract(address=Web3.to_checksum_address(feed_addr), abi=_DATA_FEED_ABI)
            # getDataInBase18 REVERTS when the feed is unhealthy — that revert is the signal.
            out["nav_usd"] = feed.functions.getDataInBase18().call(**kw) / 1e18

            aggregator_addr = feed.functions.aggregator().call(**kw)
            out["aggregator"] = aggregator_addr
            aggregator = w3.eth.contract(
                address=Web3.to_checksum_address(aggregator_addr), abi=_AGGREGATOR_ABI
            )
            _, _, _, updated_at, _ = aggregator.functions.latestRoundData().call(**kw)
            out["updated_at"] = updated_at

            # Age against the block being valued, not wall-clock — otherwise a historical read
            # always looks stale.
            reference_ts = w3.eth.get_block(block if block is not None else "latest")["timestamp"]
            out["age_s"] = max(0, reference_ts - updated_at)

            try:
                out["healthy_diff_s"] = feed.functions.healthyDiff().call(**kw)
                out["stale"] = out["age_s"] > out["healthy_diff_s"]
            except Exception:  # healthyDiff is not on every feed version
                pass
        except Exception as e:
            out["error"] = str(e)
            logger.warning("Midas NAV feed read failed for vault %s: %s", vault, e)
        return out

    def get_redemption_position(
        self,
        holders: Iterable[str],
        redemption_vaults: Iterable[str],
        rpc_url: str,
        block: Optional[int] = None,
        mtoken: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Total mToken pending redemption for `holders` across every configured vault.

        Vaults are a LIST because one mToken can have several (Midas runs more than one per product
        and adds them over time), and because their own published registry has been observed to be
        incomplete. A vault missing from config under-values silently; a vault that fails to read
        raises.

        Returns raw (wei) quantities for the caller to price — same contract as `get_grt_position`.
        """
        vaults = list(redemption_vaults)
        if not vaults:
            raise ValueError("Midas redemption position has no redemption_vaults configured")

        total_raw = 0
        resolved_mtoken: Optional[str] = None
        requests: List[Dict[str, Any]] = []
        per_vault: List[Dict[str, Any]] = []
        for vault in vaults:
            result = self.get_pending_requests(
                vault=vault, holders=holders, rpc_url=rpc_url, block=block, mtoken=mtoken
            )
            # Every vault in one position must redeem the same mToken, else the quantities being
            # summed are in different units (and priced with one token's price).
            if resolved_mtoken and result["mtoken"].lower() != resolved_mtoken.lower():
                raise ValueError(
                    f"Midas vaults disagree on mToken: {resolved_mtoken} vs {result['mtoken']} "
                    f"({result['vault']})"
                )
            resolved_mtoken = result["mtoken"]
            total_raw += result["pending_raw"]
            requests.extend(result["requests"])
            per_vault.append({
                "vault": result["vault"],
                "current_request_id": result["current_request_id"],
                "scanned": result["scanned"],
                "pending_raw": result["pending_raw"],
                "pending": result["pending_raw"] / 10 ** MTOKEN_DECIMALS,
                "pending_count": result["pending_count"],
            })

        # Health is per-vault wiring; report the first vault's feed (they share one per mToken).
        nav_feed = self.get_nav_feed_health(vaults[0], rpc_url=rpc_url, block=block)

        return {
            "pending_raw": total_raw,
            "pending": total_raw / 10 ** MTOKEN_DECIMALS,
            "pending_count": len(requests),
            "requests": requests,
            "per_vault": per_vault,
            "nav_feed": nav_feed,
            "mtoken": resolved_mtoken,
        }
