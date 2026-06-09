"""On-chain Morpho (ERC-4626) share-price client.

Reads a Morpho V1/V2 vault's share price directly from the chain instead of the
Morpho GraphQL API. ERC-4626 only exposes shares -> underlying assets
(`convertToAssets`); the underlying -> USD half is resolved here by either
treating known stablecoins as $1 or fetching the underlying's USD price from
CoinGecko (by contract address) at the block timestamp.
"""
import logging
from typing import Optional

from web3 import Web3

from avg_utility.client.coingecko_client import CoinGeckoClient

logger = logging.getLogger(__name__)

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
