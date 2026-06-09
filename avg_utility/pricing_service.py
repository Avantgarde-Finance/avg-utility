"""Pricing service — single entry point for all price operations."""
import os
import logging
from typing import Dict, List, Optional

from dotenv import load_dotenv

from avg_utility.client.coingecko_client import CoinGeckoClient
from avg_utility.client.morpho_client import MorphoClient
from avg_utility.client.morpho_onchain_client import MorphoOnchainClient
from avg_utility.client.pendle_client import PendleClient
from avg_utility.client.onyx_client import OnyxClient
from avg_utility.enum.sources import PriceSource, label_for

load_dotenv()
logger = logging.getLogger(__name__)


def _check_env():
    """Validate required environment variables and warn on missing ones."""
    api_key = os.getenv("COINGECKO_API_KEY", "")
    if not api_key:
        logger.warning(
            "COINGECKO_API_KEY is not set. CoinGecko pricing (price_source=1) will fail. "
            "Set it in your .env file or environment variables."
        )
    return api_key


DEFAULT_RPC_URLS = {
    1: "https://ethereum-rpc.publicnode.com",
    42161: "https://arbitrum-one-rpc.publicnode.com",
    8453: "https://base-rpc.publicnode.com",
    10: "https://optimism-rpc.publicnode.com",
}


class PricingService:
    """Single entry point for fetching token prices — current and historical.

    price_source mapping:
        1 = CoinGecko
        2 = Morpho V1
        3 = Pendle
        4 = Morpho V2
        5 = Onyx (on-chain sharePrice)
    """

    def __init__(self, rpc_urls: Dict[int, str] = None):
        _check_env()

        self.rpc_urls = rpc_urls or DEFAULT_RPC_URLS
        self.price_cache: Dict[str, float] = {}

        self.coingecko = CoinGeckoClient()
        self.morpho = MorphoClient()
        self.morpho_onchain = MorphoOnchainClient(coingecko=self.coingecko)
        self.pendle = PendleClient()
        self.onyx = OnyxClient()

    # ---- Current prices ----

    def get_price(
        self,
        symbol: str,
        price_source: int,
        token_address: str = None,
        chain_id: int = None,
        coingecko_id: str = None,
    ) -> Optional[float]:
        """Fetch current price for a single token."""
        if symbol in self.price_cache:
            return self.price_cache[symbol]

        price = None
        try:
            if price_source == PriceSource.COINGECKO:
                if not coingecko_id:
                    return None
                prices = self.coingecko.get_simple_price(coingecko_id)
                if coingecko_id in prices:
                    price = prices[coingecko_id]
            elif price_source == PriceSource.MORPHO_V1:
                price = self.morpho.get_current_price_v1(token_address, chain_id)
            elif price_source == PriceSource.PENDLE:
                price = self.pendle.get_current_price(token_address, chain_id)
            elif price_source == PriceSource.MORPHO_V2:
                price = self.morpho.get_current_price_v2(token_address, chain_id)
            elif price_source == PriceSource.ONYX:
                rpc_url = self.rpc_urls.get(chain_id)
                if rpc_url:
                    result = self.onyx.get_price_at_block(token_address, rpc_url)
                    if result is not None:
                        price = result[0]
            else:
                logger.warning(f"Unknown price_source {price_source} for {symbol}")
        except Exception as e:
            logger.warning(f"Failed to fetch price for {symbol} from {label_for(PriceSource, price_source)}: {e}")

        if price is not None:
            self.price_cache[symbol] = price
        return price

    def get_prices(self, tokens: list) -> Dict[str, float]:
        """Fetch current prices for multiple tokens, batching CoinGecko calls.

        Args:
            tokens: List of dicts with: symbol, price_source, coingecko_id,
                    token_address, chain_id.

        Returns:
            {symbol: usd_price}
        """
        cg_tokens = []
        other_tokens = []

        for t in tokens:
            if t.get("price_source") == PriceSource.COINGECKO:
                cg_tokens.append(t)
            else:
                other_tokens.append(t)

        # Batch CoinGecko
        if cg_tokens:
            cg_map = {t["coingecko_id"]: t["symbol"] for t in cg_tokens if t.get("coingecko_id")}
            if cg_map:
                coin_ids = ",".join(sorted(cg_map.keys()))
                try:
                    cg_prices = self.coingecko.get_simple_price(coin_ids)
                    for cg_id, symbol in cg_map.items():
                        if cg_id in cg_prices:
                            self.price_cache[symbol] = cg_prices[cg_id]
                except Exception as e:
                    logger.warning(f"CoinGecko batch fetch failed: {e}")

        for t in other_tokens:
            self.get_price(
                symbol=t["symbol"],
                price_source=t["price_source"],
                token_address=t.get("token_address"),
                chain_id=t.get("chain_id"),
                coingecko_id=t.get("coingecko_id"),
            )

        return dict(self.price_cache)

    # ---- Historical prices ----

    # Mapping from generic interval names to source-specific values
    _CG_INTERVAL = {"daily": "daily", "hourly": "hourly"}
    _MORPHO_INTERVAL = {"daily": "DAY", "hourly": "HOUR"}
    _PENDLE_INTERVAL = {"daily": "day", "hourly": "hour"}

    def get_historical_prices(
        self,
        price_source: int,
        start_timestamp: int,
        end_timestamp: int,
        coingecko_id: str = None,
        token_address: str = None,
        chain_id: int = None,
        interval: str = "daily",
    ) -> List[tuple]:
        """Fetch historical price data for a token.

        Args:
            price_source: Source ID.
            start_timestamp: Start Unix timestamp.
            end_timestamp: End Unix timestamp.
            coingecko_id: CoinGecko ID (for source 1).
            token_address: Token/vault address (for sources 2-4).
            chain_id: Chain ID (for sources 2-4).
            interval: 'daily' or 'hourly' (default: 'daily').

        Returns:
            List of (timestamp_ms_or_s, close_price) tuples.
        """
        if price_source == PriceSource.COINGECKO:
            if not coingecko_id:
                return []
            cg_interval = self._CG_INTERVAL.get(interval, "daily")
            ohlc = self.coingecko.get_price_ohlc_range(
                coingecko_id, start_timestamp, end_timestamp,
                interval=cg_interval,
            )
            return [(entry[0], entry[4]) for entry in ohlc]  # (timestamp_ms, close)

        elif price_source == PriceSource.MORPHO_V1:
            morpho_interval = self._MORPHO_INTERVAL.get(interval, "DAY")
            vault = self.morpho.get_price_share_price_usd(
                token_address, chain_id, start_timestamp, end_timestamp,
                interval=morpho_interval,
            )
            entries = vault.get("historicalState", {}).get("sharePriceUsd", [])
            return [(e["x"], float(e["y"])) for e in entries]

        elif price_source == PriceSource.PENDLE:
            from datetime import datetime
            pendle_interval = self._PENDLE_INTERVAL.get(interval, "day")
            ts_start = datetime.fromtimestamp(start_timestamp).strftime("%Y-%m-%d")
            ts_end = datetime.fromtimestamp(end_timestamp).strftime("%Y-%m-%d")
            result = self.pendle.get_price_ohlcv(
                token_address, chain_id, pendle_interval,
                timestamp_start=ts_start, timestamp_end=ts_end,
            )
            return [
                (e["time"], e["close"])
                for e in result.get("data", [])
                if start_timestamp <= e["time"] <= end_timestamp
            ]

        elif price_source == PriceSource.MORPHO_V2:
            morpho_interval = self._MORPHO_INTERVAL.get(interval, "DAY")
            vault = self.morpho.get_v2_share_price(
                token_address, chain_id, start_timestamp, end_timestamp,
                interval=morpho_interval,
            )
            entries = vault.get("historicalState", {}).get("sharePrice", [])
            return [(e["x"], float(e["y"])) for e in entries]

        else:
            logger.warning(f"No historical price support for price_source {price_source}")
            return []

    def get_price_by_block(
        self,
        price_source: int,
        token_address: str,
        chain_id: int,
        block_number: int,
    ) -> Optional[tuple]:
        """Fetch price at a specific block number (on-chain sources only).

        Args:
            price_source: Source ID. Supported: 2=Morpho V1, 4=Morpho V2,
                5=Onyx — all read on-chain (ERC-4626 / sharePrice) instead of
                via an API. Morpho sources resolve the underlying -> USD half
                by treating known stablecoins as $1 and pricing everything else
                from CoinGecko at the block timestamp.
            token_address: Token/vault address.
            chain_id: Chain ID.
            block_number: Block number to query at.

        Returns:
            (price_float, on_chain_timestamp_unix) or None.
        """
        rpc_url = self.rpc_urls.get(chain_id)
        if not rpc_url:
            logger.warning(f"No RPC URL for chain {chain_id}")
            return None

        if price_source == PriceSource.ONYX:
            return self.onyx.get_price_at_block(token_address, rpc_url, block_number)
        elif price_source in (PriceSource.MORPHO_V1, PriceSource.MORPHO_V2):
            return self.morpho_onchain.get_share_price_usd_at_block(
                vault_address=token_address,
                rpc_url=rpc_url,
                chain_id=chain_id,
                block_number=block_number,
            )
        else:
            logger.warning(f"get_price_by_block not supported for price_source {price_source}")
            return None

    def clear_cache(self):
        """Clear the in-memory price cache."""
        self.price_cache.clear()
