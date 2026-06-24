"""HorizonStaking on-chain reader — The Graph delegation positions (Arbitrum One).

Reads delegation + thawing for a (serviceProvider, verifier, delegator) and the wallet's GRT balance.
ABI lives in avg_utility/abi/HorizonStaking.json. See avg-onyx-valuation/stGRT/EXPLORATION.md.
"""
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from web3 import Web3

logger = logging.getLogger(__name__)

# ---- Constants (Arbitrum One) ----
HORIZON_STAKING = "0x00669A4CF01450B64E8A2A20E9b1FCB71E61eF03"
SUBGRAPH_SERVICE = "0xb2Bb92d0DE618878E438b55D5846cfecD9301105"  # the only `verifier` live today
GRT_TOKEN = "0x9623063377AD1B27544C965cCd7342f7EA7e88C7"
GRT_DECIMALS = 10**18

# Horizon ThawRequestType: 0=Provision, 1=Delegation, 2=DelegationWithBeneficiary
_THAW_TYPE_DELEGATION = 1
_THAW_TYPE_DELEGATION_BENEF = 2
_ZERO_BYTES32 = b"\x00" * 32

_ABI_PATH = Path(__file__).resolve().parent.parent / "abi" / "HorizonStaking.json"
_ERC20_BALANCEOF_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    }
]
# Default Arbitrum RPC; historical-block reads need an archive node.
_DEFAULT_ARB_RPC = "https://arbitrum-one-rpc.publicnode.com"


def _resolve_rpc_url(rpc_url: Optional[str]) -> str:
    if rpc_url:
        return rpc_url
    if os.getenv("ARBITRUM_RPC_URL"):
        return os.getenv("ARBITRUM_RPC_URL")
    if os.getenv("ALCHEMY_KEY"):
        return f"https://arb-mainnet.g.alchemy.com/v2/{os.getenv('ALCHEMY_KEY')}"
    return _DEFAULT_ARB_RPC


def _block_kw(block: Optional[int]) -> dict:
    return {"block_identifier": block} if block is not None else {}


class HorizonClient:
    """On-chain reads against HorizonStaking + the GRT token."""

    def __init__(self, rpc_url: Optional[str] = None, w3: Optional[Web3] = None):
        if w3 is None:
            w3 = Web3(Web3.HTTPProvider(_resolve_rpc_url(rpc_url), request_kwargs={"timeout": 30}))
        self.w3 = w3
        self.horizon = w3.eth.contract(
            address=Web3.to_checksum_address(HORIZON_STAKING),
            abi=json.loads(_ABI_PATH.read_text()),
        )
        self.grt = w3.eth.contract(
            address=Web3.to_checksum_address(GRT_TOKEN), abi=_ERC20_BALANCEOF_ABI
        )

    def is_connected(self) -> bool:
        return self.w3.is_connected()

    def get_grt_balance(self, wallet: str, block: Optional[int] = None) -> int:
        """Liquid GRT (ERC20 balanceOf), in wei."""
        return self.grt.functions.balanceOf(Web3.to_checksum_address(wallet)).call(**_block_kw(block))

    def get_delegation(self, sp: str, delegator: str, block: Optional[int] = None) -> Dict[str, Any]:
        """Active delegation for (sp, verifier, delegator): shares → GRT via the pool exchange rate.

        Returns the per-pool fields (incl. thawing pool state, needed by `get_thawing`).
        """
        kw = _block_kw(block)
        sp = Web3.to_checksum_address(sp)
        verifier = Web3.to_checksum_address(SUBGRAPH_SERVICE)
        delegator = Web3.to_checksum_address(delegator)

        user_shares = self.horizon.functions.getDelegation(sp, verifier, delegator).call(**kw)[0]
        pool = self.horizon.functions.getDelegationPool(sp, verifier).call(**kw)
        pool_tokens, pool_shares, pool_tokens_thawing, pool_shares_thawing, pool_thawing_nonce = pool

        # `pool_tokens` from getDelegationPool INCLUDES the thawing reserve, while `pool_shares`
        # excludes thawing shares. Using the raw ratio overstates the per-share GRT value for pools
        # with a large thawing pool. Net out the thawing tokens to get the active-delegation rate.
        active_tokens = pool_tokens - pool_tokens_thawing
        delegated = (user_shares * active_tokens // pool_shares) if (pool_shares and user_shares) else 0
        return {
            "sp": sp,
            "user_shares": user_shares,
            "pool_tokens": pool_tokens,
            "pool_shares": pool_shares,
            "pool_tokens_thawing": pool_tokens_thawing,
            "pool_shares_thawing": pool_shares_thawing,
            "pool_thawing_nonce": pool_thawing_nonce,
            "delegated": delegated,
        }

    def get_thawing(self, pos: Dict[str, Any], delegator: str, block: Optional[int] = None) -> int:
        """Sum NON-stale thaw requests (in GRT wei) for the pool described by `pos`.

        Stale requests (`thawingNonce != pool.thawingNonce`, post-slash) are unwithdrawable → excluded.
        """
        kw = _block_kw(block)
        sp = Web3.to_checksum_address(pos["sp"])
        verifier = Web3.to_checksum_address(SUBGRAPH_SERVICE)
        delegator = Web3.to_checksum_address(delegator)

        total = 0
        for req_type in (_THAW_TYPE_DELEGATION, _THAW_TYPE_DELEGATION_BENEF):
            try:
                head, _tail, _nonce, count = self.horizon.functions.getThawRequestList(
                    req_type, sp, verifier, delegator
                ).call(**kw)
            except Exception:
                continue
            if count == 0 or bytes(head) == _ZERO_BYTES32:
                continue
            current = bytes(head)
            seen = 0
            while current != _ZERO_BYTES32 and seen < int(count) + 5:
                try:
                    shares, _until, next_request, thaw_nonce = self.horizon.functions.getThawRequest(
                        req_type, current
                    ).call(**kw)
                except Exception:
                    break
                if pos["pool_shares_thawing"] > 0:
                    grt = shares * pos["pool_tokens_thawing"] // pos["pool_shares_thawing"]
                else:
                    grt = 0
                if thaw_nonce == pos["pool_thawing_nonce"]:  # skip stale (slashed)
                    total += grt
                current = bytes(next_request)
                seen += 1
        return total
