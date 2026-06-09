"""The Graph gateway (network subgraph) client — GraphQL queries via the decentralized gateway."""
import logging
import os
from typing import Any, Dict, List, Optional

import requests
from web3 import Web3

logger = logging.getLogger(__name__)


class TheGraphClient:
    """Thin client for The Graph's decentralized gateway.

    Auth is a gateway API key (THEGRAPH_API_KEY) passed path-style. Used today for discovering a
    delegator's service-providers off-chain (HorizonStaking has no enumerable getter).
    """

    GATEWAY = "https://gateway.thegraph.com/api/{key}/subgraphs/id/{sid}"
    # Graph Network subgraph (Arbitrum One)
    NETWORK_SUBGRAPH_ARBITRUM = "DZz4kDTdmzWLWsV373w2bSmoar3umKKH9y82SUKr5qmp"

    def __init__(self, api_key: Optional[str] = None, timeout: int = 30):
        self.api_key = api_key or os.getenv("THEGRAPH_API_KEY")
        self.timeout = timeout

    def query(self, subgraph_id: str, query: str, variables: Optional[Dict] = None) -> Dict[str, Any]:
        """POST a GraphQL query to a subgraph and return its `data`. Raises on auth/GraphQL errors."""
        if not self.api_key:
            raise RuntimeError("THEGRAPH_API_KEY not set — cannot query The Graph gateway.")
        url = self.GATEWAY.format(key=self.api_key, sid=subgraph_id)
        resp = requests.post(
            url, json={"query": query, "variables": variables or {}}, timeout=self.timeout
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("errors"):
            raise RuntimeError(f"Subgraph errors: {payload['errors']}")
        return payload.get("data") or {}

    def discover_delegation_service_providers(self, delegator: str) -> List[str]:
        """Return every service-provider (indexer) the delegator has a stake entity with.

        Deliberately does NOT filter on active shares — a fully-undelegated position has 0 active
        shares but may still have GRT thawing; the on-chain reads decide the real state.
        """
        query = """
        query($delegator: ID!) {
          delegator(id: $delegator) {
            id
            stakes { indexer { id } }
          }
        }
        """
        data = self.query(
            self.NETWORK_SUBGRAPH_ARBITRUM, query, {"delegator": delegator.lower()}
        )
        delegator_obj = data.get("delegator")
        if not delegator_obj:
            return []
        sps = [
            Web3.to_checksum_address(stake["indexer"]["id"])
            for stake in delegator_obj.get("stakes", [])
            if (stake.get("indexer") or {}).get("id")
        ]
        return sorted(set(sps))
