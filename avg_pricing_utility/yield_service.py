"""Yield service — single entry point for all yield/APY operations."""
import logging
from datetime import datetime
from typing import Dict, List, Optional

from avg_pricing_utility.client.defillama_client import DefiLlamaClient
from avg_pricing_utility.client.morpho_client import MorphoClient
from avg_pricing_utility.client.pendle_client import PendleClient

logger = logging.getLogger(__name__)

SOURCE_NAMES = {
    1: "DefiLlama",
    2: "Morpho V1",
    3: "Pendle",
    4: "Morpho V2",
}


class YieldService:
    """Single entry point for fetching yield/APY data.

    yield_source mapping:
        1 = DefiLlama   (pool APY via dlid)
        2 = Morpho V1   (vault dailyApy)
        3 = Pendle       (market impliedApy)
        4 = Morpho V2   (vault avgApy)
    """

    def __init__(self):
        self.defillama = DefiLlamaClient()
        self.morpho = MorphoClient()
        self.pendle = PendleClient()
        self._pendle_markets_cache: Optional[Dict] = None

    def _get_pendle_markets(self, chain_id: str = "1") -> Dict:
        """Get Pendle markets with caching."""
        if self._pendle_markets_cache is None:
            self._pendle_markets_cache = self.pendle.get_pendle_markets(chain_id=chain_id)
        return self._pendle_markets_cache

    def _find_pendle_market_address(self, token_address: str, chain_id: str = "1") -> Optional[str]:
        """Find Pendle market address by pt/yt/sy token address."""
        markets_data = self._get_pendle_markets(chain_id)
        markets = markets_data.get("markets", [])

        search_address = token_address
        if "-" in token_address:
            search_address = token_address.split("-", 1)[1]

        for market in markets:
            for key in ("pt", "yt", "sy"):
                addr = market.get(key, "")
                if addr and "-" in addr:
                    if addr.split("-", 1)[1].lower() == search_address.lower():
                        return market.get("address")
        return None

    def get_historical_yields(
        self,
        yield_source: int,
        start_timestamp: int,
        end_timestamp: int,
        token_address: str = None,
        chain_id: int = None,
        dlid: str = None,
    ) -> List[tuple]:
        """Fetch historical yield data for a token.

        Args:
            yield_source: Source ID.
            start_timestamp: Start Unix timestamp.
            end_timestamp: End Unix timestamp.
            token_address: Token/vault address (for sources 2-4).
            chain_id: Chain ID (for sources 2-4).
            dlid: DefiLlama pool ID (for source 1).

        Returns:
            List of (datetime, apy_float) tuples, sorted by date.
        """
        records = []

        try:
            if yield_source == 1:
                records = self._fetch_defillama(dlid, start_timestamp, end_timestamp)
            elif yield_source == 2:
                records = self._fetch_morpho_v1_apy(token_address, chain_id, start_timestamp, end_timestamp)
            elif yield_source == 3:
                records = self._fetch_pendle_apy(token_address, chain_id, start_timestamp, end_timestamp)
            elif yield_source == 4:
                records = self._fetch_morpho_v2_apy(token_address, chain_id, start_timestamp, end_timestamp)
            else:
                logger.warning(f"Unknown yield_source {yield_source}")
                return []
        except Exception as e:
            logger.warning(f"Failed to fetch yield from {SOURCE_NAMES.get(yield_source, yield_source)}: {e}")
            return []

        # Forward-fill negative APYs
        records.sort(key=lambda r: r[0])
        for i in range(1, len(records)):
            if records[i][1] < 0:
                records[i] = (records[i][0], records[i - 1][1])

        return records

    def _fetch_defillama(self, dlid: str, start_ts: int, end_ts: int) -> List[tuple]:
        if not dlid:
            return []
        yield_data = self.defillama.get_yield_data(dlid)
        records = []
        for entry in yield_data:
            timestamp_str = entry["timestamp"]
            timestamp_clean = timestamp_str.split(".")[0].replace("T", " ")
            entry_dt = datetime.strptime(timestamp_clean, "%Y-%m-%d %H:%M:%S")
            entry_date = entry_dt.replace(hour=0, minute=0, second=0, microsecond=0)
            entry_ts = int(entry_date.timestamp())
            if start_ts <= entry_ts <= end_ts:
                apy = entry.get("apy")
                if apy is not None:
                    records.append((entry_date, float(apy) / 100.0))
        return records

    def _fetch_morpho_v1_apy(self, address: str, chain_id: int, start_ts: int, end_ts: int) -> List[tuple]:
        vault = self.morpho.get_daily_apy(address, chain_id, start_ts, end_ts, "DAY")
        entries = vault.get("historicalState", {}).get("dailyApy", [])
        records = []
        for e in entries:
            ts = e["x"]
            if start_ts <= ts <= end_ts:
                apy = e.get("y")
                if apy is not None:
                    entry_date = datetime.fromtimestamp(ts).replace(hour=0, minute=0, second=0, microsecond=0)
                    records.append((entry_date, float(apy)))
        return records

    def _fetch_morpho_v2_apy(self, address: str, chain_id: int, start_ts: int, end_ts: int) -> List[tuple]:
        vault = self.morpho.get_v2_daily_apy(address, chain_id, start_ts, end_ts, "DAY")
        entries = vault.get("historicalState", {}).get("avgApy", [])
        records = []
        for e in entries:
            ts = e["x"]
            if start_ts <= ts <= end_ts:
                apy = e.get("y")
                if apy is not None:
                    entry_date = datetime.fromtimestamp(ts).replace(hour=0, minute=0, second=0, microsecond=0)
                    records.append((entry_date, float(apy)))
        return records

    def _fetch_pendle_apy(self, token_address: str, chain_id: int, start_ts: int, end_ts: int) -> List[tuple]:
        market_address = self._find_pendle_market_address(token_address, str(chain_id))
        if not market_address:
            logger.warning(f"Could not find Pendle market for token {token_address}")
            return []

        start_date = datetime.fromtimestamp(start_ts).strftime("%Y-%m-%d")
        end_date = datetime.fromtimestamp(end_ts).strftime("%Y-%m-%d")

        apy_data = self.pendle.get_pendle_market_apy(
            market_address, start_date=start_date, end_date=end_date,
            chain_id=chain_id, time_frame="day",
        )

        records = []
        for entry in apy_data.get("results", []):
            timestamp_str = entry.get("timestamp", "")
            if not timestamp_str:
                continue
            timestamp_clean = timestamp_str.split(".")[0].replace("T", " ")
            entry_dt = datetime.strptime(timestamp_clean, "%Y-%m-%d %H:%M:%S")
            entry_date = entry_dt.replace(hour=0, minute=0, second=0, microsecond=0)
            entry_ts = int(entry_date.timestamp())
            if start_ts <= entry_ts <= end_ts:
                implied_apy = entry.get("impliedApy")
                if implied_apy is not None:
                    records.append((entry_date, float(implied_apy)))
        return records
