"""CoinGecko Pro API client."""
import os
import requests
from typing import Optional, List, Dict

from dotenv import load_dotenv

load_dotenv()


class CoinGeckoClient:
    """Client for CoinGecko Pro API."""

    BASE_URL = "https://pro-api.coingecko.com/api/v3"

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("COINGECKO_API_KEY", "")
        self.headers = {
            "accept": "application/json",
            "x-cg-pro-api-key": self.api_key,
        }

    def get_simple_price(self, coin_ids: str) -> Dict[str, float]:
        """Fetch current prices via /simple/price.

        Args:
            coin_ids: Comma-separated CoinGecko IDs.

        Returns:
            {coingecko_id: usd_price}
        """
        url = f"{self.BASE_URL}/simple/price"
        params = {"ids": coin_ids, "vs_currencies": "usd"}

        response = requests.get(url, params=params, headers=self.headers, timeout=10)
        response.raise_for_status()
        data = response.json()

        return {cg_id: info["usd"] for cg_id, info in data.items() if "usd" in info}

    def get_price_ohlc(self, coin: str, vs_currency: str = "usd",
                       interval: str = "daily", days: str = "max") -> List[List]:
        """Get OHLC price data for a coin.

        Args:
            coin: CoinGecko coin ID (e.g. 'ethereum').
            vs_currency: Quote currency (default: 'usd').
            interval: 'daily' or 'hourly'.
            days: Number of days — '1', '7', '14', '30', '90', '180', 'max'.

        Returns:
            [[timestamp_ms, open, high, low, close], ...]
        """
        url = f"{self.BASE_URL}/coins/{coin}/ohlc"
        params = {"vs_currency": vs_currency, "interval": interval, "days": days}

        response = requests.get(url, headers=self.headers, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        if isinstance(data, list):
            return data
        raise Exception(f"API error: {data.get('error', 'Unknown error')}")

    def get_price_ohlc_range(self, coin: str, from_timestamp: int, to_timestamp: int,
                             vs_currency: str = "usd", interval: str = "daily") -> List[List]:
        """Get OHLC price data within a specific time range.

        Args:
            coin: CoinGecko coin ID.
            from_timestamp: Start Unix timestamp.
            to_timestamp: End Unix timestamp.
            vs_currency: Quote currency (default: 'usd').
            interval: 'daily' or 'hourly'.

        Returns:
            [[timestamp_ms, open, high, low, close], ...]
        """
        url = f"{self.BASE_URL}/coins/{coin}/ohlc/range"
        params = {
            "vs_currency": vs_currency,
            "from": from_timestamp,
            "to": to_timestamp,
            "interval": interval,
        }

        response = requests.get(url, headers=self.headers, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        if isinstance(data, list):
            return data
        raise Exception(f"API error: {data.get('error', 'Unknown error')}")
