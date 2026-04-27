# Investment Assistant + Research Assistant — slide replications

Replicates the deterministic primitives shown on two slides from the same talk:

- *Investment Assistant* — the Discord bot ("MCB") that, given a natural-language question, looks up the current gold/copper ratio and runs a 200-DMA cross backtest.
- *Research Assistant* — the autoresearch loop that, after ~91,000 experiments, produced an optimised SPX panic-recovery strategy. This repo contains the **final rules** as a runnable backtest + state machine, not the search loop itself.

## Files

- [`investment_assistant.py`](./investment_assistant.py) — DB layer, all tool functions, chart renderers, and JSON tool schemas (`QUERY_MARKET_DATA_SCHEMA`, `BACKTEST_SCHEMA`, `CHAMPION_SIGNAL_SCHEMA`).
- [`replicate.ipynb`](./replicate.ipynb) — Investment Assistant slide: seed → Round 1 (naive ratio query → 0 rows) → Round 2 (two-subquery rewrite) → Round 3 (10Y SMA-200 backtest).
- [`champion_signal.ipynb`](./champion_signal.ipynb) — Research Assistant slide: implements the optimised rules (alert: 42d ROC < −12% AND VIX > 28 for ≥2 days; entry: close > 5d MA AND VIX ≥ 26 within 20d; min hold 63d; 12% trailing stop; max hold 504d), runs a backtest on SPY + ^VIX, and reports the **current state** (idle / armed / in-trade) so a "live" caller knows what the bot would say today.

The Discord wrapper, the Claude API loop, prompt caching, and the autoresearch search loop aren't here — they're the orchestration that sits on top of these primitives.

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

champion_signal_backtest(
    asset_ticker="SPY", vix_ticker="^VIX",
    # all rule parameters overridable for sensitivity analysis
    roc_threshold=-0.12, vix_alert_threshold=28.0, alert_persistence=2,
    confirmation_window=20, ma_window=5, vix_entry_threshold=26.0,
    min_hold_days=63, trailing_stop=0.12, max_hold_days=504,
    target_horizon=126, target_returns=(0.15, 0.20),
)
# ChampionSignalResult(series, trades, metrics, state_now, chart_path, params)

champion_signal_state(asset_ticker="SPY", vix_ticker="^VIX")
# {'state': 'idle'|'armed'|'in_trade', 'as_of': ..., 'asset_close': ...,
#  'vix': ..., 'roc_42d': ..., 'alert_check': {...} | 'entry_check': {...}
#  | 'distance_to_trailing_stop': ..., 'days_held': ..., ...}
```
