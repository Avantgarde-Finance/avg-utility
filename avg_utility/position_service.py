"""Position service — single entry point for on-chain position reads.

Mirrors PricingService / YieldService: a `position_source` id dispatches to a registered reader for
positions that are NOT a plain ERC20 `balanceOf` (e.g. native Graph delegation, which mints no token).

position_source mapping (see avg_utility.enum.sources.PositionSource):
    1 = Graph Horizon Delegation  (Arbitrum One; wallet + delegated + thawing, in GRT)
"""
import logging
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from avg_utility.client.horizon_client import HorizonClient
from avg_utility.client.thegraph_client import TheGraphClient
from avg_utility.enum.sources import PositionSource, label_for

load_dotenv()
logger = logging.getLogger(__name__)


class PositionService:
    """Fetch on-chain positions by `position_source`.

    Clients are created lazily so importing/constructing the service is cheap and side-effect free
    (no RPC/subgraph calls until a read is requested).
    """

    def __init__(
        self,
        rpc_urls: Optional[Dict[int, str]] = None,
        thegraph_api_key: Optional[str] = None,
    ):
        self.rpc_urls = rpc_urls or {}
        self._thegraph_api_key = thegraph_api_key
        self._horizon: Optional[HorizonClient] = None
        self._thegraph: Optional[TheGraphClient] = None

    # ---- lazy clients ----

    def _horizon_client(self) -> HorizonClient:
        if self._horizon is None:
            self._horizon = HorizonClient(rpc_url=self.rpc_urls.get(42161))
        return self._horizon

    def _thegraph_client(self) -> TheGraphClient:
        if self._thegraph is None:
            self._thegraph = TheGraphClient(api_key=self._thegraph_api_key)
        return self._thegraph

    # ---- dispatch ----

    def get_position(self, position_source: int, wallet: str, block: Optional[int] = None, **kwargs) -> Dict[str, Any]:
        """Dispatch to the reader for `position_source`. Raises on unknown source."""
        if position_source == PositionSource.GRAPH_HORIZON_DELEGATION:
            return self.get_grt_position(wallet, block=block, **kwargs)
        raise ValueError(f"Unknown position_source {position_source} ({label_for(PositionSource, position_source)})")

    # ---- readers ----

    def get_grt_position(
        self,
        wallet: str,
        block: Optional[int] = None,
        service_providers: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """The Graph (Horizon) delegation position for a wallet, denominated in GRT.

        Args:
            wallet: delegator address.
            block: block to pin all on-chain reads to (None = latest).
            service_providers: explicit (sp) list; if omitted, discovered via the network subgraph.

        Returns (all GRT amounts as integer wei, 18 dec):
            {
                wallet, delegated, thawing, total,        # total = wallet + delegated + thawing
                delegated_plus_thawing,                   # the non-tokenized "position" portion
                per_pool: [{sp, delegated, thawing}, ...],
                service_providers: [...],
            }
        """
        horizon = self._horizon_client()
        if not horizon.is_connected():
            raise ConnectionError("Failed to connect to Arbitrum RPC")

        sps = service_providers or self._thegraph_client().discover_delegation_service_providers(wallet)

        wallet_grt = horizon.get_grt_balance(wallet, block)

        total_delegated = 0
        total_thawing = 0
        per_pool: List[Dict[str, Any]] = []
        for sp in sps:
            pos = horizon.get_delegation(sp, wallet, block)
            thawing = horizon.get_thawing(pos, wallet, block)
            total_delegated += pos["delegated"]
            total_thawing += thawing
            if pos["delegated"] or thawing:
                per_pool.append({"sp": pos["sp"], "delegated": pos["delegated"], "thawing": thawing})

        return {
            "wallet": wallet_grt,
            "delegated": total_delegated,
            "thawing": total_thawing,
            "total": wallet_grt + total_delegated + total_thawing,
            "delegated_plus_thawing": total_delegated + total_thawing,
            "per_pool": per_pool,
            "service_providers": sps,
        }
