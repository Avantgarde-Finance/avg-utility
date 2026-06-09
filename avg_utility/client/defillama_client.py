"""DefiLlama API client for yield data."""
import requests
from typing import Dict, List


class DefiLlamaClient:
    """Client for DefiLlama Yields API."""

    API_URL = "https://yields.llama.fi/chart"

    def get_yield_data(self, pool: str) -> List[Dict]:
        """Get yield data for a pool.

        Args:
            pool: Pool ID (e.g. 'aa70268e-4b52-42bf-a116-608b370f9501').

        Returns:
            List of dicts with: timestamp, tvlUsd, apy, apyBase,
            apyReward, il7d, apyBase7d.
        """
        url = f"{self.API_URL}/{pool}"

        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()

        if "data" in data:
            return data["data"]
        raise Exception(f"API error: {data.get('error', 'Unknown error')}")
