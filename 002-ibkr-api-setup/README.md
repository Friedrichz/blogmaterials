# IBKR API Connection Setup

Guide and starter code for connecting to Interactive Brokers (IBKR) via Python,
designed to complement the ETF replicator project in `001-etf-replicator/`.

## API Options

IBKR offers three API interfaces:

| | TWS API | Client Portal API | Web API |
|---|---|---|---|
| **Protocol** | Async socket | REST (local Java gateway) | REST + WebSocket |
| **Requires TWS/Gateway** | Yes | Java gateway | No (cloud) |
| **Maturity** | Most mature | Established | Newest |
| **Real-time data** | Streaming | Polling | WebSocket |
| **Best for** | Algorithmic trading | Light REST access | Commercial apps |

**Recommendation**: Use the **TWS API** via `ib_insync` for our ETF replicator use case.
It's the most battle-tested, full-featured, and best suited for programmatic trading.

## Prerequisites

1. **IBKR Account** — Pro account required (Lite doesn't support API).
   A [paper trading account](https://www.interactivebrokers.com/en/trading/papertrading.php) works for development.
2. **TWS or IB Gateway** — Download from [IBKR](https://www.interactivebrokers.com/en/trading/tws.php).
   IB Gateway is lighter-weight (headless) and recommended for automated systems.
3. **Python 3.9+**

## TWS/Gateway Configuration

1. Open TWS (or IB Gateway)
2. Navigate to **Edit → Global Configuration → API → Settings**
3. Check **"Enable ActiveX and Socket Clients"**
4. Set **Trusted IP**: `127.0.0.1` (for local connections)
5. Note the **port**:
   - **7496** — TWS live trading
   - **7497** — TWS paper trading
   - **4001** — IB Gateway live trading
   - **4002** — IB Gateway paper trading
6. Uncheck **"Read-Only API"** if you want to place orders (keep it checked while testing)

## Quick Start

```bash
cd 002-ibkr-api-setup
pip install -r requirements.txt
```

### 1. Test Connection

```python
python ibkr_connect.py
```

This verifies you can connect and fetches your account summary.

### 2. Fetch Market Data

```python
python ibkr_market_data.py
```

Fetches real-time quotes and historical bars for a sample set of stocks.

### 3. Place Orders (Paper Trading)

```python
python ibkr_orders.py
```

Demonstrates bracket orders, limit orders, and portfolio-based order generation
(matching the ETF replicator output).

## Architecture

```
ibkr_connect.py       — Connection helper and account info
ibkr_market_data.py   — Market data: quotes, bars, streaming
ibkr_orders.py        — Order placement and management
ibkr_portfolio.py     — Portfolio sync with ETF replicator weights
```

## Key Concepts

### Connection Management
- `ib_insync` wraps the TWS API into a synchronous-feeling Python interface
- Always call `ib.disconnect()` when done
- Use `clientId` to differentiate multiple connections (0-31)

### Rate Limits
- Max 8 simultaneous client connections
- Market data: 100 simultaneous subscriptions (more with paid data)
- Historical data: pacing limits (~6 requests/2 seconds for same ticker)
- Order placement: no hard limit but be reasonable

### Paper vs Live
- Use paper trading (port 7497/4002) for all development
- The code is identical — only the port changes
- Paper accounts reset nightly and start with $1M virtual cash

## References

- [IBKR API Overview](https://www.interactivebrokers.com/en/trading/ib-api.php)
- [TWS API Python Guide](https://www.interactivebrokers.com/campus/ibkr-quant-news/interactive-brokers-python-api-native-a-step-by-step-guide/)
- [ib_insync Documentation](https://ib-insync.readthedocs.io/)
- [ib_insync GitHub](https://github.com/erdewit/ib_insync)
- [TWS API Source Code](https://www.interactivebrokers.com/campus/trading-lessons/accessing-the-tws-python-api-source-code/)
