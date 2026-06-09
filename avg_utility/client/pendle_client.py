"""Pendle API client for token prices, OHLCV, and market APY data."""
import csv
from io import StringIO
from typing import Dict, Optional
from datetime import datetime

import requests


class PendleClient:
    """Client for Pendle API."""

    API_URL = "https://api-v2.pendle.finance/core/v4"

    def get_price_ohlcv(self, token_address: str, chain_id: int = 1,
                        time_frame: str = "day",
                        timestamp_start: Optional[str] = None,
                        timestamp_end: Optional[str] = None) -> Dict:
        """Get OHLCV price data for a Pendle token.

        Args:
            token_address: Pendle token address.
            chain_id: Chain ID (default: 1).
            time_frame: 'day' or 'hour' (default: 'day').
            timestamp_start: ISO date string filter (e.g. '2025-12-01').
            timestamp_end: ISO date string filter (e.g. '2025-12-06').

        Returns:
            {'metadata': {...}, 'data': [{'time': ts, 'open': ..., 'high': ..., 'low': ..., 'close': ..., 'volume': ...}, ...]}
        """
        url = f"{self.API_URL}/{chain_id}/prices/{token_address}/ohlcv"
        params = {"time_frame": time_frame}
        if timestamp_start:
            params["timestamp_start"] = timestamp_start
        if timestamp_end:
            params["timestamp_end"] = timestamp_end

        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        metadata = {
            "total": data.get("total"),
            "currency": data.get("currency"),
            "timeFrame": data.get("timeFrame"),
            "timestamp_start": data.get("timestamp_start"),
            "timestamp_end": data.get("timestamp_end"),
        }

        ohlcv_data = []
        csv_data = data.get("results", "")
        if csv_data:
            for row in csv.DictReader(StringIO(csv_data)):
                ohlcv_data.append({
                    "time": int(row["time"]),
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": float(row.get("volume", 0)),
                })

        return {"metadata": metadata, "data": ohlcv_data}

    def get_pendle_markets(self, chain_id: str = "1") -> Dict:
        """Get all Pendle markets."""
        url = "https://api-v2.pendle.finance/core/v1/markets/all"
        params = {"chainId": chain_id}

        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        return response.json()

    def get_pendle_market_apy(
        self, address: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        chain_id: int = 1,
        time_frame: str = "day",
    ) -> Dict:
        """Get historical APY data for a Pendle market."""
        if start_date is None:
            start_date = "2025-01-01"
        if end_date is None:
            end_date = datetime.strftime(datetime.today(), "%Y-%m-%d")

        url = f"https://api-v2.pendle.finance/core/v2/{chain_id}/markets/{address}/historical-data"
        fields = (
            "timestamp,maxApy,baseApy,underlyingApy,impliedApy,tvl,totalTvl,"
            "underlyingInterestApy,underlyingRewardApy,ytFloatingApy,swapFeeApy,"
            "voterApr,pendleApy,lpRewardApy,totalPt,totalSy,totalSupply,ptPrice,"
            "ytPrice,syPrice,lpPrice,lastEpochVotes,tradingVolume"
        )
        params = {
            "time_frame": time_frame,
            "timestamp_start": start_date,
            "timestamp_end": end_date,
            "fields": fields,
            "includeFeeBreakdown": "true",
        }

        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        return response.json()

    # --- Convenience: current price ---

    def get_current_price(self, token_address: str, chain_id: int) -> Optional[float]:
        """Get latest close price for a Pendle token."""
        result = self.get_price_ohlcv(token_address, chain_id, "day")
        data = result.get("data", [])
        return float(data[-1]["close"]) if data else None
