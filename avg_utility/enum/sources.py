"""Source-id enums for pricing, yield, and position reads.

These are `IntEnum`s on purpose: the values are persisted as INTEGER columns in the DB
(`holdings_data.token_universe.price_source` / `yield_source` / `position_source`). Because an
`IntEnum` *is* an `int`, code can keep passing/comparing raw ints from the DB unchanged
(`price_source == PriceSource.COINGECKO` works whether the input is `1` or the enum member).

⚠️ The integer values are a persistence contract — never renumber an existing member; only append.
"""
from enum import IntEnum


class PriceSource(IntEnum):
    COINGECKO = 1
    MORPHO_V1 = 2
    PENDLE = 3
    MORPHO_V2 = 4
    ONYX = 5

    @property
    def label(self) -> str:
        return {1: "CoinGecko", 2: "Morpho V1", 3: "Pendle", 4: "Morpho V2", 5: "Onyx"}[self.value]


class YieldSource(IntEnum):
    DEFILLAMA = 1
    MORPHO_V1 = 2
    PENDLE = 3
    MORPHO_V2 = 4

    @property
    def label(self) -> str:
        return {1: "DefiLlama", 2: "Morpho V1", 3: "Pendle", 4: "Morpho V2"}[self.value]


class PositionSource(IntEnum):
    GRAPH_HORIZON_DELEGATION = 1
    MORPHO_LOOP = 2

    @property
    def label(self) -> str:
        return {1: "Graph Horizon Delegation", 2: "Morpho Loop"}[self.value]


def label_for(enum_cls, value) -> str:
    """Human label for a source value; falls back to the raw value for unknown ids (safe for logging)."""
    try:
        return enum_cls(value).label
    except (ValueError, KeyError):
        return str(value)
