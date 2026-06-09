"""Morpho GraphQL API client for vault prices and APY data."""
import time
import requests
from typing import Optional, Dict


class MorphoClient:
    """Client for Morpho GraphQL API — supports V1 and V2 vaults."""

    API_URL = "https://api.morpho.org/graphql"

    def _query(self, query: str, variables: dict) -> dict:
        resp = requests.post(
            self.API_URL,
            json={"query": query, "variables": variables},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            raise Exception(data["errors"][0].get("message", "Unknown error"))
        return data.get("data", {})

    def _build_options(self, start_timestamp: Optional[int] = None,
                       end_timestamp: Optional[int] = None,
                       interval: str = "DAY") -> dict:
        options = {"interval": interval}
        if start_timestamp:
            options["startTimestamp"] = start_timestamp
        if end_timestamp:
            options["endTimestamp"] = end_timestamp
        return options

    # --- V1 vaults ---

    def get_price_share_price_usd(
        self, address: str, chain_id: int = 1,
        start_timestamp: Optional[int] = None,
        end_timestamp: Optional[int] = None,
        interval: str = "DAY",
    ) -> Dict:
        """Get share price USD data for a Morpho V1 vault."""
        query = """
        query($address: String!, $chainId: Int!, $options: TimeseriesOptions) {
          vaultByAddress(address: $address, chainId: $chainId) {
            address
            historicalState { sharePriceUsd(options: $options) { x y } }
            creationTimestamp
          }
        }
        """
        options = self._build_options(start_timestamp, end_timestamp, interval)
        data = self._query(query, {"address": address, "chainId": chain_id, "options": options})
        vault = data.get("vaultByAddress")
        if not vault:
            raise Exception("No vault data returned")
        return vault

    def get_daily_apy(
        self, address: str, chain_id: int = 1,
        start_timestamp: Optional[int] = None,
        end_timestamp: Optional[int] = None,
        interval: str = "DAY",
    ) -> Dict:
        """Get daily APY data for a Morpho V1 vault."""
        query = """
        query($address: String!, $chainId: Int!, $options: TimeseriesOptions) {
          vaultByAddress(address: $address, chainId: $chainId) {
            address
            historicalState { dailyApy(options: $options) { x y } }
            creationTimestamp
          }
        }
        """
        options = self._build_options(start_timestamp, end_timestamp, interval)
        data = self._query(query, {"address": address, "chainId": chain_id, "options": options})
        vault = data.get("vaultByAddress")
        if not vault:
            raise Exception("No vault data returned")
        return vault

    # --- V2 vaults ---

    def get_v2_share_price(
        self, address: str, chain_id: int = 1,
        start_timestamp: Optional[int] = None,
        end_timestamp: Optional[int] = None,
        interval: str = "DAY",
    ) -> Dict:
        """Get share price data for a Morpho V2 vault."""
        query = """
        query VaultV2ByAddress($address: String!, $chainId: Int!, $options: TimeseriesOptions) {
          vaultV2ByAddress(address: $address, chainId: $chainId) {
            address
            historicalState { sharePrice(options: $options) { x y } }
            creationTimestamp
          }
        }
        """
        options = self._build_options(start_timestamp, end_timestamp, interval)
        data = self._query(query, {"address": address, "chainId": chain_id, "options": options})
        vault = data.get("vaultV2ByAddress")
        if not vault:
            raise Exception("No vault data returned")
        return vault

    def get_v2_daily_apy(
        self, address: str, chain_id: int = 1,
        start_timestamp: Optional[int] = None,
        end_timestamp: Optional[int] = None,
        interval: str = "DAY",
    ) -> Dict:
        """Get daily APY data for a Morpho V2 vault."""
        query = """
        query VaultV2ByAddress($address: String!, $chainId: Int!, $options: TimeseriesOptions) {
          vaultV2ByAddress(address: $address, chainId: $chainId) {
            address
            historicalState { avgApy(options: $options) { x y } }
            creationTimestamp
          }
        }
        """
        options = self._build_options(start_timestamp, end_timestamp, interval)
        data = self._query(query, {"address": address, "chainId": chain_id, "options": options})
        vault = data.get("vaultV2ByAddress")
        if not vault:
            raise Exception("No vault data returned")
        return vault

    # --- Convenience: current price ---

    def get_current_price_v1(self, address: str, chain_id: int) -> Optional[float]:
        """Get latest share price USD for a V1 vault."""
        now = int(time.time())
        vault = self.get_price_share_price_usd(address, chain_id, now - 86400, now)
        entries = vault.get("historicalState", {}).get("sharePriceUsd", [])
        return float(entries[-1]["y"]) if entries else None

    def get_current_price_v2(self, address: str, chain_id: int) -> Optional[float]:
        """Get latest share price for a V2 vault."""
        now = int(time.time())
        vault = self.get_v2_share_price(address, chain_id, now - 86400, now)
        entries = vault.get("historicalState", {}).get("sharePrice", [])
        return float(entries[-1]["y"]) if entries else None
