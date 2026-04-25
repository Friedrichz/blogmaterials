# Investment Assistant — slide replication

Replicates the deterministic primitives shown on the *Investment Assistant* slide:

> **User:** "What's the current gold/copper ratio? Backtest buying when it crosses above the 200 DMA over the last 10 years."

The slide describes a Discord bot ("MCB") backed by a Claude API tool-use loop that orchestrates two tools across three rounds:

| Round | Action |
|---|---|
| 1 | `query_market_data` with `WHERE ticker = 'GC=F/HG=F'` → 0 rows (ratio tickers aren't stored directly) |
| 2 | Self-correction: rewrite as two subqueries `(gold close) / (copper close)` → the current ratio |
| 3 | `backtest` with `tickers='GC=F/HG=F'`, `entry_condition='price_cross_above_sma_200'`, `exit_condition='price_cross_below_sma_200'`, `days=3650`, `timeframe='daily'` |

This repo contains **just the two tools** — `query_market_data` and `backtest` — plus a notebook that walks through the three rounds end-to-end. The Discord wrapper, the Claude API loop, and prompt caching aren't here; they're the orchestration layer that sits on top.

## Files

- [`investment_assistant.py`](./investment_assistant.py) — DB layer, the two tool functions, the chart renderer, and the JSON tool schemas (`QUERY_MARKET_DATA_SCHEMA`, `BACKTEST_SCHEMA`).
- [`replicate.ipynb`](./replicate.ipynb) — runs the slide example: seed → Round 1 → Round 2 → Round 3, with the chart inline.

## Run it

```sh
pip install -r requirements.txt
jupyter notebook replicate.ipynb
```

`seed_ohlcv(..., source="auto")` (the default) tries Yahoo Finance first and falls back to a deterministic synthetic generator if the network can't reach it. With internet access you get the real numbers; in a sandboxed/offline environment you get a plausible synthetic series so the plumbing still exercises end-to-end.

## What the tools accept

```python
query_market_data(sql)
# {'columns': [...], 'rows': [...], 'row_count': N}

backtest(
    tickers="GC=F/HG=F",                            # 'A' or 'A/B' (ratio)
    entry_condition="price_cross_above_sma_200",    # price_cross_{above,below}_sma_<N>
    exit_condition="price_cross_below_sma_200",
    days=3650,
    timeframe="daily",
)
# BacktestResult(series, trades, metrics, chart_path, ...)
```

The `backtest` chart matches the slide layout: price + SMA + buy/sell markers in the top panel, equity curve overlay, drawdown subplot, metrics table at the bottom.
