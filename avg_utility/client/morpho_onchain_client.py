"""On-chain Morpho reads: ERC-4626 vault share prices and Morpho Blue loop positions.

Two readers, both hitting the chain instead of the Morpho GraphQL API:

* `get_share_price_usd_at_block` — a Morpho V1/V2 vault's share price. ERC-4626 only exposes shares
  -> underlying assets (`convertToAssets`); the underlying -> USD half is resolved here by treating
  known stablecoins as $1 or fetching the underlying's USD price from CoinGecko (by contract) at the
  block timestamp.
* `get_loop_position` — a leveraged position on a single Morpho Blue market (supply collateral,
  borrow the borrow token, re-supply to lever up). It mints no share token to `balanceOf` and its
  value is composite (collateral minus debt), so this reads both legs from the Morpho Blue singleton
  and marks net equity to USD purely on-chain (stablecoin leg = $1, other leg via the market oracle).
  (Morpho's `MarketParams` names the borrowed asset ``loanToken``; we call it the borrow token.)
"""
import json
import logging
from pathlib import Path
from typing import Optional

from web3 import Web3

from avg_utility.client.coingecko_client import CoinGeckoClient

logger = logging.getLogger(__name__)

_ABI_DIR = Path(__file__).resolve().parent.parent / "abi"

# Minimal ERC-4626 + ERC20 ABI — only the reads we need.
_ERC4626_ABI = [
    {"name": "asset", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "address"}]},
    {"name": "decimals", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "uint8"}]},
    {"name": "convertToAssets", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "shares", "type": "uint256"}], "outputs": [{"type": "uint256"}]},
]

# CoinGecko asset-platform slugs keyed by chain id.
_CG_PLATFORM = {
    1: "ethereum",
    42161: "arbitrum-one",
    8453: "base",
    10: "optimistic-ethereum",
}

# Known USD-pegged stablecoins per chain (lowercased addresses) — treated as $1.
_STABLECOINS = {
    1: {
        "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",  # USDC
        "0xdac17f958d2ee523a2206206994597c13d831ec7",  # USDT
        "0x6b175474e89094c44da98b954eedeac495271d0f",  # DAI
        "0xdc035d45d973e3ec169d2276ddab16f1e407384f",  # USDS
    },
    42161: {
        "0xaf88d065e77c8cc2239327c5edb3a432268e5831",  # USDC (native)
        "0xff970a61a04b1ca14834a43f5de4533ebddb5cc8",  # USDC.e (bridged)
        "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9",  # USDT
        "0xda10009cbd5d07dd0cecc66161fc93d7c9000da1",  # DAI
    },
    8453: {
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC (native)
        "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca",  # USDbC (bridged)
        "0x50c5725949a6f0c72e6c4a641f24049a917db0cb",  # DAI
    },
}

# ----- Morpho Blue loop-position constants -----

# Canonical Morpho Blue singleton — same CREATE2 address on Ethereum, Base, Optimism, Arbitrum.
# Chains that deployed it elsewhere must pass `morpho_address` explicitly.
DEFAULT_MORPHO_BLUE = "0xBBBBBbbBBb9cC5e90e3b3Af64bdAF62C37EEFFCb"

ORACLE_PRICE_SCALE = 10 ** 36
VIRTUAL_SHARES = 10 ** 6
VIRTUAL_ASSETS = 1

# Morpho Blue + IOracle ABIs (reads only; ERC20 `decimals` reuses _ERC4626_ABI).
_MORPHO_BLUE_ABI = json.loads((_ABI_DIR / "MorphoBlue.json").read_text())
_ORACLE_ABI = json.loads((_ABI_DIR / "MorphoOracle.json").read_text())


def _mul_div_down(a: int, b: int, c: int) -> int:
    return (a * b) // c


def _mul_div_up(a: int, b: int, c: int) -> int:
    return (a * b + (c - 1)) // c


def _to_assets_down(shares: int, total_assets: int, total_shares: int) -> int:
    """SharesMathLib.toAssetsDown — shares → assets, rounding down (supply side)."""
    return _mul_div_down(shares, total_assets + VIRTUAL_ASSETS, total_shares + VIRTUAL_SHARES)


def _to_assets_up(shares: int, total_assets: int, total_shares: int) -> int:
    """SharesMathLib.toAssetsUp — shares → assets, rounding up (debt side)."""
    return _mul_div_up(shares, total_assets + VIRTUAL_ASSETS, total_shares + VIRTUAL_SHARES)


class MorphoOnchainClient:
    """Fetches Morpho vault share prices in USD via on-chain ERC-4626 reads."""

    def __init__(self, coingecko: Optional[CoinGeckoClient] = None):
        self.coingecko = coingecko or CoinGeckoClient()
        self._w3_cache: dict = {}
        self._decimals_cache: dict = {}

    def _get_web3(self, rpc_url: str) -> Web3:
        if rpc_url not in self._w3_cache:
            self._w3_cache[rpc_url] = Web3(Web3.HTTPProvider(rpc_url))
        return self._w3_cache[rpc_url]

    def _erc20_decimals(self, w3: Web3, address: str) -> int:
        key = address.lower()
        if key not in self._decimals_cache:
            contract = w3.eth.contract(
                address=Web3.to_checksum_address(address), abi=_ERC4626_ABI
            )
            self._decimals_cache[key] = contract.functions.decimals().call()
        return self._decimals_cache[key]

    def get_share_price_usd_at_block(
        self,
        vault_address: str,
        rpc_url: str,
        chain_id: int,
        block_number: Optional[int] = None,
    ) -> Optional[tuple]:
        """Read a Morpho vault's share price in USD, optionally at a block.

        Args:
            vault_address: ERC-4626 Morpho vault (share token) address.
            rpc_url: RPC endpoint (use an archive node for historical blocks).
            chain_id: Chain ID the vault lives on.
            block_number: Block to query at. None for latest.

        Returns:
            (price_usd_float, onchain_timestamp_unix) or None on error.
        """
        try:
            w3 = self._get_web3(rpc_url)
            vault = w3.eth.contract(
                address=Web3.to_checksum_address(vault_address), abi=_ERC4626_ABI
            )
            block_identifier = block_number if block_number is not None else "latest"

            share_decimals = vault.functions.decimals().call(block_identifier=block_identifier)
            assets_raw = vault.functions.convertToAssets(10 ** share_decimals).call(
                block_identifier=block_identifier
            )
            underlying_address = vault.functions.asset().call(block_identifier=block_identifier)
            underlying_decimals = self._erc20_decimals(w3, underlying_address)

            # Price of one share denominated in the underlying asset.
            price_in_underlying = assets_raw / (10 ** underlying_decimals)

            block = w3.eth.get_block(block_identifier)
            onchain_ts = int(block["timestamp"])

            underlying_usd = self._underlying_usd_price(
                underlying_address, chain_id, onchain_ts
            )
            if underlying_usd is None:
                logger.warning(
                    f"Could not resolve USD price for underlying {underlying_address} "
                    f"(chain {chain_id}); cannot compute Morpho share USD price on-chain"
                )
                return None

            price_usd = price_in_underlying * underlying_usd
            logger.info(
                f"On-chain Morpho share price: {price_in_underlying:,.6f} underlying "
                f"x ${underlying_usd:,.6f} = ${price_usd:,.6f} "
                f"(vault {vault_address}, block {block_identifier})"
            )
            return price_usd, onchain_ts
        except Exception as e:
            logger.error(
                f"Failed on-chain Morpho share price for {vault_address} "
                f"{f'at block {block_number}' if block_number else '(latest)'}: {e}"
            )
            return None

    def _underlying_usd_price(
        self, underlying_address: str, chain_id: int, timestamp_unix: int
    ) -> Optional[float]:
        """Resolve the underlying asset's USD price.

        Stablecoins are treated as $1; everything else is priced from CoinGecko
        (by contract address) at the closest point to ``timestamp_unix``.
        """
        addr_lower = underlying_address.lower()
        if addr_lower in _STABLECOINS.get(chain_id, set()):
            return 1.0

        platform = _CG_PLATFORM.get(chain_id)
        if not platform:
            logger.warning(f"No CoinGecko platform mapping for chain {chain_id}")
            return None

        prices = self.coingecko.get_market_chart_contract_range(
            platform, addr_lower, timestamp_unix - 3600, timestamp_unix + 3600,
        )
        if not prices:
            logger.warning(
                f"CoinGecko returned no prices for {addr_lower} on {platform} near {timestamp_unix}"
            )
            return None

        # Closest data point to the requested timestamp.
        target_ms = timestamp_unix * 1000
        closest = min(prices, key=lambda p: abs(p[0] - target_ms))
        return float(closest[1])

    # ----- Morpho Blue loop positions -----

    def get_loop_position(
        self,
        wallet: str,
        market_id: str,
        rpc_url: str,
        chain_id: int,
        morpho_address: Optional[str] = None,
        block: Optional[int] = None,
    ) -> dict:
        """Net equity of a wallet's loop on one Morpho Blue market, marked to USD on-chain.

        Terminology: Morpho's `MarketParams` calls the debt asset ``loanToken``; because in a loop
        you *borrow* it, this reader names it the **borrow token** throughout to avoid confusion.

        Args:
            wallet: position owner (the product's defi_wallet).
            market_id: Morpho Blue market id (bytes32 hex string, ``0x…``).
            rpc_url: RPC endpoint for `chain_id` (archive node for historical blocks).
            chain_id: chain the market lives on.
            morpho_address: Morpho Blue singleton; defaults to the canonical CREATE2 address.
            block: block to pin all reads to (None = latest).

        Returns (raw amounts are native token wei; ``*_borrow`` are borrow-token units):
            {
                collateral, supply_assets, borrow_assets,             # native raw ints
                collateral_value_borrow_raw, net_equity_borrow_raw,   # borrow-token raw ints
                net_equity_borrow, borrow_token_usd, usd_value,        # floats
                collateral_price_in_borrow, collateral_price_usd,      # oracle-implied collateral price
                price_method,                                          # how borrow_token_usd was resolved
                borrow_token, collateral_token, borrow_decimals, collateral_decimals,
                oracle_price, market_id,
            }
        """
        w3 = self._get_web3(rpc_url)
        block_id = block if block is not None else "latest"
        morpho = w3.eth.contract(
            address=Web3.to_checksum_address(morpho_address or DEFAULT_MORPHO_BLUE),
            abi=_MORPHO_BLUE_ABI,
        )
        mid = market_id if isinstance(market_id, (bytes, bytearray)) else Web3.to_bytes(hexstr=market_id)
        user = Web3.to_checksum_address(wallet)

        # Morpho's MarketParams.loanToken is the asset you borrow — the "borrow token" here.
        borrow_token, collateral_token, oracle_addr, _irm, _lltv = \
            morpho.functions.idToMarketParams(mid).call(block_identifier=block_id)
        supply_shares, borrow_shares, collateral = \
            morpho.functions.position(mid, user).call(block_identifier=block_id)
        (total_supply_assets, total_supply_shares,
         total_borrow_assets, total_borrow_shares, _last_update, _fee) = \
            morpho.functions.market(mid).call(block_identifier=block_id)

        supply_assets = _to_assets_down(supply_shares, total_supply_assets, total_supply_shares) \
            if supply_shares else 0
        borrow_assets = _to_assets_up(borrow_shares, total_borrow_assets, total_borrow_shares) \
            if borrow_shares else 0

        # Read the market oracle unconditionally — it prices the collateral leg AND, when the
        # collateral is a stablecoin, anchors the borrow token to USD (see _resolve_borrow_usd).
        oracle = w3.eth.contract(address=Web3.to_checksum_address(oracle_addr), abi=_ORACLE_ABI)
        oracle_price = oracle.functions.price().call(block_identifier=block_id)
        collateral_value_borrow_raw = (collateral * oracle_price) // ORACLE_PRICE_SCALE

        net_equity_borrow_raw = supply_assets + collateral_value_borrow_raw - borrow_assets

        borrow_decimals = self._erc20_decimals(w3, borrow_token)
        collateral_decimals = self._erc20_decimals(w3, collateral_token)
        net_equity_borrow = net_equity_borrow_raw / (10 ** borrow_decimals)

        borrow_token_usd, price_method = self._resolve_borrow_usd(
            borrow_token, collateral_token, oracle_price, borrow_decimals, collateral_decimals, chain_id,
        )
        usd_value = net_equity_borrow * borrow_token_usd if borrow_token_usd is not None else None

        # Oracle-implied price of the collateral asset: borrow tokens per 1 whole collateral token
        # (oracle_price is 1e36-scaled and folds in the decimal gap), then in USD via the borrow
        # token's price. Lets consumers report/reconcile the collateral mark without re-deriving it.
        collateral_price_in_borrow = (oracle_price / ORACLE_PRICE_SCALE) * (10 ** collateral_decimals) / (10 ** borrow_decimals)
        collateral_price_usd = collateral_price_in_borrow * borrow_token_usd if borrow_token_usd is not None else None

        logger.info(
            "Morpho loop %s market %s: net equity %.6f borrow-token x $%s (%s) = %s "
            "(collateral=%s, borrow_assets=%s)",
            wallet, market_id, net_equity_borrow,
            f"{borrow_token_usd:,.6f}" if borrow_token_usd is not None else "N/A",
            price_method,
            f"${usd_value:,.2f}" if usd_value is not None else "N/A",
            collateral, borrow_assets,
        )

        return {
            "collateral": collateral,
            "supply_assets": supply_assets,
            "borrow_assets": borrow_assets,
            "collateral_value_borrow_raw": collateral_value_borrow_raw,
            "net_equity_borrow_raw": net_equity_borrow_raw,
            "net_equity_borrow": net_equity_borrow,
            "borrow_token_usd": borrow_token_usd,
            "collateral_price_in_borrow": collateral_price_in_borrow,
            "collateral_price_usd": collateral_price_usd,
            "price_method": price_method,
            "usd_value": usd_value,
            "borrow_token": borrow_token,
            "collateral_token": collateral_token,
            "borrow_decimals": borrow_decimals,
            "collateral_decimals": collateral_decimals,
            "oracle_price": oracle_price,
            "market_id": market_id,
        }

    def _resolve_borrow_usd(
        self,
        borrow_token: str,
        collateral_token: str,
        oracle_price: int,
        borrow_decimals: int,
        collateral_decimals: int,
        chain_id: int,
    ):
        """USD price of the borrow token, anchored on-chain to the market's stablecoin leg.

          1. borrow token is a USD stablecoin  → $1
          2. collateral is a USD stablecoin    → invert the market oracle (fully on-chain)
          3. neither leg is a stablecoin       → unpriced (None); can't anchor to USD on-chain

        Returns ``(price, method)``. Case 2: the oracle (collateral→borrow, 1e36-scaled) pins the
        borrow token's USD price — 1 collateral = $1 = price·10^(collDec−borrowDec)/1e36 borrow
        tokens, so 1 borrow token = 1e36·10^(borrowDec−collDec)/price USD.
        """
        stables = _STABLECOINS.get(chain_id, set())

        if borrow_token.lower() in stables:
            return 1.0, "stablecoin"

        if collateral_token.lower() in stables and oracle_price:
            borrow_usd = (ORACLE_PRICE_SCALE / oracle_price) * (10 ** borrow_decimals) / (10 ** collateral_decimals)
            return borrow_usd, "oracle_inverse"

        logger.warning(
            "Morpho loop market has no stablecoin leg (borrow %s / collateral %s on chain %s); "
            "cannot anchor USD on-chain — returning unpriced",
            borrow_token, collateral_token, chain_id,
        )
        return None, "unpriced"
