# Avantgarde Utility

Shared pricing and yield utility for AVG projects.

## Installation

```bash
# uv
uv add git+https://github.com/Avantgarde-Finance/avg-utility.git

# pip
pip install git+https://github.com/Avantgarde-Finance/avg-utility.git
```

### Upgrading

```bash
# uv
uv add "git+https://github.com/Avantgarde-Finance/avg-utility.git" --upgrade

# pip
pip install --upgrade git+https://github.com/Avantgarde-Finance/avg-utility.git
```

## Configuration

Set `COINGECKO_API_KEY` in your `.env` file or environment variables. The service will warn at initialization if the key is missing.

## References

- **CoinGecko IDs**: https://docs.google.com/spreadsheets/d/1wTTuxXt8n9q7C4NDXqQpI3wpKu1_5bGVmP9Xz0XGSyU/edit?gid=0#gid=0

---

## PricingService

```python
from avg_pricing_utility import PricingService

service = PricingService()
```

### Supported Sources

| price_source | Source | Current Price Endpoint | Historical Price Endpoint |
|---|---|---|---|
| 1 | CoinGecko | `GET /simple/price` | `GET /coins/{id}/ohlc/range` |
| 2 | Morpho V1 | `POST /graphql` → `vaultByAddress.sharePriceUsd` | Same (with date range) |
| 3 | Pendle | `GET /v4/{chainId}/prices/{address}/ohlcv` | Same (with date range) |
| 4 | Morpho V2 | `POST /graphql` → `vaultV2ByAddress.sharePrice` | Same (with date range) |

### `get_price()` — current price for a single token

```python
price = service.get_price(symbol="USDC", price_source=1, coingecko_id="usd-coin")
```

| Parameter | Required | Description |
|---|---|---|
| `symbol` | Yes | Token symbol (cache key) |
| `price_source` | Yes | Source ID (1-4) |
| `coingecko_id` | For source 1 | CoinGecko coin ID |
| `token_address` | For sources 2-4 | Token/vault contract address |
| `chain_id` | For sources 2-4 | Chain ID |

### `get_prices()` — current prices for multiple tokens

CoinGecko tokens are automatically grouped into a single API call.

```python
prices = service.get_prices([
    {"symbol": "USDC", "price_source": 1, "coingecko_id": "usd-coin"},
    {"symbol": "WETH", "price_source": 1, "coingecko_id": "weth"},
    {"symbol": "AVGUSDCcons", "price_source": 2, "token_address": "0x...", "chain_id": 1},
])
# Returns: {"USDC": 1.0, "WETH": 2600.0, "AVGUSDCcons": 1.02}
```

### `get_historical_prices()` — historical price data

```python
prices = service.get_historical_prices(
    price_source=1,
    start_timestamp=1700000000,
    end_timestamp=1700600000,
    coingecko_id="ethereum",
)
# Returns [(timestamp, close_price), ...]
```

Use `interval="hourly"` for higher granularity:

```python
prices = service.get_historical_prices(
    price_source=2,
    start_timestamp=target_ts - 3600,
    end_timestamp=target_ts + 3600,
    token_address="0x...",
    chain_id=1,
    interval="hourly",
)
```

| Parameter | Required | Default | Description |
|---|---|---|---|
| `price_source` | Yes | | Source ID (1-4) |
| `start_timestamp` | Yes | | Start Unix timestamp |
| `end_timestamp` | Yes | | End Unix timestamp |
| `coingecko_id` | For source 1 | | CoinGecko coin ID |
| `token_address` | For sources 2-4 | | Token/vault contract address |
| `chain_id` | For sources 2-4 | | Chain ID |
| `interval` | No | `"daily"` | `"daily"` or `"hourly"` |

---

## YieldService

```python
from avg_pricing_utility import YieldService

service = YieldService()
```

### Supported Sources

| yield_source | Source | Endpoint |
|---|---|---|
| 1 | DefiLlama | `GET https://yields.llama.fi/chart/{pool_id}` |
| 2 | Morpho V1 | `POST /graphql` → `vaultByAddress.dailyApy` |
| 3 | Pendle | `GET /v2/{chainId}/markets/{address}/historical-data` → `impliedApy` |
| 4 | Morpho V2 | `POST /graphql` → `vaultV2ByAddress.avgApy` |

### `get_historical_yields()`

```python
yields = service.get_historical_yields(
    yield_source=2,
    start_timestamp=1700000000,
    end_timestamp=1700600000,
    token_address="0x...",
    chain_id=1,
)
# Returns [(datetime, apy), ...]
```

Negative APY values are automatically forward-filled with the previous day's value.

| Parameter | Required | Description |
|---|---|---|
| `yield_source` | Yes | Source ID (1-4) |
| `start_timestamp` | Yes | Start Unix timestamp |
| `end_timestamp` | Yes | End Unix timestamp |
| `token_address` | For sources 2-4 | Token/vault contract address |
| `chain_id` | For sources 2-4 | Chain ID |
| `dlid` | For source 1 | DefiLlama pool ID |
