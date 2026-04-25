"""Investment Assistant primitives.

Replicates the two tools from the "Investment Assistant" slide:

* `query_market_data(sql)` — read-only SQL over an `ohlcv` table.
* `backtest(...)` — walk-forward backtest with SMA-cross entry/exit rules,
  supports ratio tickers like ``GC=F/HG=F`` (the syntax the slide demonstrates),
  and emits a chart + metrics dict.

Plus a `seed_ohlcv()` helper that pulls daily history from yfinance into a
SQLite DB so the tools have something to read.

This is the deterministic core. The slide also shows a Claude API loop
orchestrating these tools across multiple rounds; that orchestration layer is
not included here.
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import yfinance as yf

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
CHARTS_DIR = ROOT / "charts"
DEFAULT_DB_PATH = DATA_DIR / "market.db"

# Default universe — the slide uses GC=F (gold futures) and HG=F (copper
# futures); the rest are useful for exploration.
DEFAULT_TICKERS: tuple[str, ...] = (
    "GC=F",  # Gold futures
    "HG=F",  # Copper futures
    "CL=F",  # WTI crude
    "SPY",   # S&P 500 ETF
    "TLT",   # 20Y treasuries ETF
    "DX=F",  # US Dollar Index futures
)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

OHLCV_SCHEMA = """
CREATE TABLE IF NOT EXISTS ohlcv (
    ticker TEXT NOT NULL,
    date   TEXT NOT NULL,
    open   REAL,
    high   REAL,
    low    REAL,
    close  REAL,
    volume INTEGER,
    PRIMARY KEY (ticker, date)
);
CREATE INDEX IF NOT EXISTS idx_ohlcv_ticker_date ON ohlcv (ticker, date);
"""


def connect(db_path: str | os.PathLike[str] = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open the SQLite DB, creating the schema if needed."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(OHLCV_SCHEMA)
    return conn


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------

def seed_ohlcv(
    tickers: tuple[str, ...] | list[str] = DEFAULT_TICKERS,
    period: str = "15y",
    db_path: str | os.PathLike[str] = DEFAULT_DB_PATH,
    source: str = "auto",
) -> dict[str, int]:
    """Populate the OHLCV table.

    ``source`` is ``"yfinance"`` (real data), ``"synthetic"`` (deterministic
    plausible series — useful when the network can't reach Yahoo), or
    ``"auto"`` (try yfinance, fall back to synthetic on error).

    Returns ``{ticker: rows_inserted}``.
    """
    if source not in {"auto", "yfinance", "synthetic"}:
        raise ValueError(f"unknown source {source!r}")

    if source == "synthetic":
        counts = _seed_synthetic(tickers, period, db_path)
        print(f"[seed] source=synthetic  rows={counts}")
        return counts

    counts = _seed_yfinance(tickers, period, db_path)
    if source == "yfinance" or any(v > 0 for v in counts.values()):
        print(f"[seed] source=yfinance  rows={counts}")
        return counts
    # Auto fallback: yfinance got nothing for every ticker (offline, blocked,
    # rate-limited). Use synthetic data instead.
    counts = _seed_synthetic(tickers, period, db_path)
    print(f"[seed] source=synthetic (yfinance unavailable)  rows={counts}")
    return counts


def _seed_yfinance(
    tickers: tuple[str, ...] | list[str],
    period: str,
    db_path: str | os.PathLike[str],
) -> dict[str, int]:
    conn = connect(db_path)
    counts: dict[str, int] = {}
    # yfinance prints HTTP errors directly to stderr and via the root logger;
    # silence both for the duration of the download. Errors still surface as
    # an empty DataFrame, which the caller handles.
    yf_log_level = logging.getLogger("yfinance").level
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    try:
        for ticker in tickers:
            try:
                with contextlib.redirect_stderr(io.StringIO()), \
                        contextlib.redirect_stdout(io.StringIO()):
                    df = yf.download(
                        ticker,
                        period=period,
                        interval="1d",
                        auto_adjust=False,
                        progress=False,
                        threads=False,
                    )
            except Exception:
                df = None
            if df is None or df.empty:
                counts[ticker] = 0
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = df.reset_index().rename(
                columns={
                    "Date": "date",
                    "Open": "open",
                    "High": "high",
                    "Low": "low",
                    "Close": "close",
                    "Volume": "volume",
                }
            )
            df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
            df["ticker"] = ticker
            rows = df[["ticker", "date", "open", "high", "low", "close", "volume"]]
            conn.executemany(
                "INSERT OR REPLACE INTO ohlcv (ticker, date, open, high, low, close, volume) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows.itertuples(index=False, name=None),
            )
            counts[ticker] = len(rows)
        conn.commit()
    finally:
        conn.close()
        logging.getLogger("yfinance").setLevel(yf_log_level)
    return counts


# Plausible starting prices and annualized vol/drift per ticker. The GC/HG
# starting prices and vol roughly match late-2025 levels so the ratio number
# the notebook prints is in the right ballpark even with synthetic data.
_SYNTHETIC_PARAMS: dict[str, dict[str, float]] = {
    "GC=F": {"start": 2400.0, "drift": 0.06, "vol": 0.15, "factor": 0.55},
    "HG=F": {"start": 4.30, "drift": 0.04, "vol": 0.22, "factor": 0.65},
    "CL=F": {"start": 80.0, "drift": 0.02, "vol": 0.32, "factor": 0.50},
    "SPY":  {"start": 480.0, "drift": 0.09, "vol": 0.16, "factor": 0.80},
    "TLT":  {"start": 95.0, "drift": 0.00, "vol": 0.13, "factor": -0.30},
    "DX=F": {"start": 104.0, "drift": 0.00, "vol": 0.07, "factor": -0.25},
}


def _period_to_days(period: str) -> int:
    period = period.strip().lower()
    m = re.match(r"^(\d+)([dmy])$", period)
    if not m:
        raise ValueError(f"unsupported period {period!r}")
    n, unit = int(m.group(1)), m.group(2)
    return {"d": n, "m": n * 30, "y": n * 365}[unit]


def _seed_synthetic(
    tickers: tuple[str, ...] | list[str],
    period: str,
    db_path: str | os.PathLike[str],
) -> dict[str, int]:
    """Generate a deterministic OHLCV history for each ticker.

    Returns serve as a reasonable proxy for end-to-end testing — real
    correlations and levels obviously won't match. Each ticker's daily log
    return is ``factor * market_shock + idiosyncratic``, so GC and HG share
    some commodity-like drift.
    """
    days = _period_to_days(period)
    end = pd.Timestamp.today().normalize()
    business_days = pd.bdate_range(end=end, periods=int(days * 252 / 365))
    n = len(business_days)

    rng = np.random.default_rng(seed=42)
    # Shared market-factor shocks (zero-mean) — drives correlation.
    market = rng.normal(loc=0.0, scale=1.0, size=n)

    conn = connect(db_path)
    counts: dict[str, int] = {}
    try:
        for ticker in tickers:
            params = _SYNTHETIC_PARAMS.get(ticker)
            if params is None:
                # Unknown ticker — give it generic parameters.
                params = {"start": 100.0, "drift": 0.05, "vol": 0.20, "factor": 0.50}

            daily_drift = params["drift"] / 252.0
            daily_vol = params["vol"] / np.sqrt(252.0)
            factor = params["factor"]

            idio = rng.normal(loc=0.0, scale=1.0, size=n)
            shock = factor * market + np.sqrt(max(1.0 - factor**2, 0.0)) * idio
            log_returns = daily_drift + daily_vol * shock
            close = params["start"] * np.exp(np.cumsum(log_returns))

            # Build OHLC around the close — simple but plausible.
            intraday = rng.normal(loc=0.0, scale=daily_vol * 0.5, size=n)
            open_ = close * np.exp(-intraday)
            high = np.maximum(open_, close) * np.exp(np.abs(rng.normal(0.0, daily_vol * 0.3, n)))
            low = np.minimum(open_, close) * np.exp(-np.abs(rng.normal(0.0, daily_vol * 0.3, n)))
            volume = rng.integers(low=10_000, high=1_000_000, size=n)

            df = pd.DataFrame(
                {
                    "ticker": ticker,
                    "date": business_days.strftime("%Y-%m-%d"),
                    "open": open_,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": volume,
                }
            )
            conn.executemany(
                "INSERT OR REPLACE INTO ohlcv (ticker, date, open, high, low, close, volume) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                df.itertuples(index=False, name=None),
            )
            counts[ticker] = len(df)
        conn.commit()
    finally:
        conn.close()
    return counts


# ---------------------------------------------------------------------------
# Tool 1: query_market_data
# ---------------------------------------------------------------------------

# Block anything that mutates state. SQLite's authorizer would be the proper
# fix, but a regex on the leading keyword + a forbid-list covers the obvious
# cases for an LLM-driven query tool.
_ALLOWED_LEADING = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)
_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|ATTACH|DETACH|PRAGMA|VACUUM)\b",
    re.IGNORECASE,
)


def query_market_data(
    sql: str,
    db_path: str | os.PathLike[str] = DEFAULT_DB_PATH,
    max_rows: int = 1000,
) -> dict[str, Any]:
    """Run a read-only SELECT against the OHLCV database.

    Mirrors the tool surface from the slide. Returns a dict with ``columns``
    and ``rows`` so callers (a Claude tool loop, a notebook, a test) can
    consume it uniformly.
    """
    if not _ALLOWED_LEADING.match(sql):
        return {"error": "only SELECT / WITH queries are allowed"}
    if _FORBIDDEN.search(sql):
        return {"error": "query contains a forbidden keyword"}

    conn = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True)
    try:
        cur = conn.execute(sql)
        columns = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(max_rows)
    finally:
        conn.close()

    return {
        "columns": columns,
        "rows": [list(r) for r in rows],
        "row_count": len(rows),
    }


# ---------------------------------------------------------------------------
# Tool 2: backtest
# ---------------------------------------------------------------------------

# Match conditions like "price_cross_above_sma_200" or "..._below_sma_50".
_CONDITION_RE = re.compile(
    r"^price_cross_(?P<dir>above|below)_sma_(?P<window>\d+)$",
    re.IGNORECASE,
)


@dataclass
class BacktestResult:
    series: pd.DataFrame  # date-indexed: price, sma, position, equity, bh_equity, drawdown
    trades: pd.DataFrame  # one row per round-trip: entry/exit dates and prices, return
    metrics: dict[str, float]
    chart_path: Path
    tickers: str
    entry_condition: str
    exit_condition: str
    days: int


def _load_close_series(
    ticker: str,
    days: int,
    db_path: str | os.PathLike[str],
) -> pd.Series:
    """Load the close series for a single ticker, restricted to the lookback."""
    conn = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True)
    try:
        df = pd.read_sql_query(
            "SELECT date, close FROM ohlcv WHERE ticker = ? ORDER BY date",
            conn,
            params=(ticker,),
            parse_dates=["date"],
        )
    finally:
        conn.close()
    if df.empty:
        raise ValueError(f"no rows for ticker {ticker!r}")
    df = df.set_index("date")["close"].astype(float).dropna()
    if days > 0:
        cutoff = df.index.max() - pd.Timedelta(days=days)
        df = df[df.index >= cutoff]
    return df


def _resolve_price_series(
    tickers: str,
    days: int,
    db_path: str | os.PathLike[str],
) -> pd.Series:
    """Resolve a ticker expression to a price series.

    Supports a single ticker (``GC=F``) or a ratio (``GC=F/HG=F``).
    The slide explicitly demonstrates the ratio form — that's why it's here.
    """
    if "/" in tickers:
        num, den = [t.strip() for t in tickers.split("/", 1)]
        a = _load_close_series(num, days, db_path)
        b = _load_close_series(den, days, db_path)
        idx = a.index.intersection(b.index)
        if idx.empty:
            raise ValueError(
                f"no overlapping dates between {num!r} and {den!r}"
            )
        return (a.loc[idx] / b.loc[idx]).rename(tickers)
    return _load_close_series(tickers.strip(), days, db_path).rename(tickers)


def _parse_condition(label: str) -> tuple[str, int]:
    m = _CONDITION_RE.match(label.strip())
    if not m:
        raise ValueError(
            f"unsupported condition {label!r} — "
            "expected e.g. 'price_cross_above_sma_200'"
        )
    return m.group("dir").lower(), int(m.group("window"))


def _max_drawdown(equity: pd.Series) -> float:
    running_max = equity.cummax()
    drawdown = equity / running_max - 1.0
    return float(drawdown.min())


def backtest(
    tickers: str,
    entry_condition: str,
    exit_condition: str,
    days: int = 3650,
    timeframe: str = "daily",
    db_path: str | os.PathLike[str] = DEFAULT_DB_PATH,
    charts_dir: str | os.PathLike[str] = CHARTS_DIR,
    chart_filename: str | None = None,
) -> BacktestResult:
    """Run a long-only backtest with SMA-cross entry/exit rules.

    Parameters mirror the tool the slide demonstrates:
    ``tickers`` (supports ratio syntax), ``entry_condition`` /
    ``exit_condition`` (``price_cross_{above,below}_sma_<N>``), ``days`` and
    ``timeframe``.
    """
    if timeframe != "daily":
        raise ValueError(f"unsupported timeframe {timeframe!r} — only 'daily'")

    entry_dir, entry_window = _parse_condition(entry_condition)
    exit_dir, exit_window = _parse_condition(exit_condition)

    price = _resolve_price_series(tickers, days, db_path)
    if len(price) < max(entry_window, exit_window) + 5:
        raise ValueError("not enough history for the requested SMA windows")

    sma_entry = price.rolling(entry_window, min_periods=entry_window).mean()
    sma_exit = price.rolling(exit_window, min_periods=exit_window).mean()

    # Detect crossings: yesterday on one side, today on the other.
    above_entry = price > sma_entry
    cross_up_entry = above_entry & ~above_entry.shift(1).fillna(False)
    cross_down_entry = ~above_entry & above_entry.shift(1).fillna(False)
    above_exit = price > sma_exit
    cross_up_exit = above_exit & ~above_exit.shift(1).fillna(False)
    cross_down_exit = ~above_exit & above_exit.shift(1).fillna(False)

    enter = cross_up_entry if entry_dir == "above" else cross_down_entry
    exit_ = cross_up_exit if exit_dir == "above" else cross_down_exit

    position = pd.Series(0, index=price.index, dtype=int)
    in_pos = False
    entry_idx: pd.Timestamp | None = None
    trades: list[dict[str, Any]] = []

    for ts in price.index:
        if not in_pos and bool(enter.loc[ts]):
            in_pos = True
            entry_idx = ts
        elif in_pos and bool(exit_.loc[ts]):
            in_pos = False
            entry_price = float(price.loc[entry_idx])
            exit_price = float(price.loc[ts])
            trades.append(
                {
                    "entry_date": entry_idx,
                    "entry_price": entry_price,
                    "exit_date": ts,
                    "exit_price": exit_price,
                    "return": exit_price / entry_price - 1.0,
                }
            )
            entry_idx = None
        position.loc[ts] = 1 if in_pos else 0

    # Equity: strategy is long when position == 1 (using next-day return is
    # purer; here we use same-day daily return because positions are evaluated
    # at the close — close-to-close is fine for a daily bar slide demo).
    daily_return = price.pct_change().fillna(0.0)
    strat_return = daily_return * position.shift(1).fillna(0).astype(float)
    equity = (1.0 + strat_return).cumprod()
    bh_equity = (1.0 + daily_return).cumprod()
    drawdown = equity / equity.cummax() - 1.0

    series = pd.DataFrame(
        {
            "price": price,
            "sma_entry": sma_entry,
            "sma_exit": sma_exit,
            "position": position,
            "equity": equity,
            "bh_equity": bh_equity,
            "drawdown": drawdown,
        }
    )
    trades_df = pd.DataFrame(trades)

    metrics = _compute_metrics(equity, bh_equity, trades_df, days)

    charts_dir = Path(charts_dir)
    charts_dir.mkdir(parents=True, exist_ok=True)
    if chart_filename is None:
        safe_tickers = tickers.replace("/", "_over_").replace("=", "")
        chart_filename = f"{safe_tickers}_{entry_window}d_{days}d.png"
    chart_path = charts_dir / chart_filename

    _render_chart(
        series=series,
        trades=trades_df,
        metrics=metrics,
        tickers=tickers,
        entry_condition=entry_condition,
        exit_condition=exit_condition,
        out_path=chart_path,
    )

    return BacktestResult(
        series=series,
        trades=trades_df,
        metrics=metrics,
        chart_path=chart_path,
        tickers=tickers,
        entry_condition=entry_condition,
        exit_condition=exit_condition,
        days=days,
    )


def _compute_metrics(
    equity: pd.Series,
    bh_equity: pd.Series,
    trades: pd.DataFrame,
    days: int,
) -> dict[str, float]:
    n = len(equity)
    years = n / 252.0 if n else 0.0
    total_return = float(equity.iloc[-1] - 1.0) if n else 0.0
    bh_total_return = float(bh_equity.iloc[-1] - 1.0) if n else 0.0

    def _cagr(end: float) -> float:
        if years <= 0 or end <= 0:
            return 0.0
        return (end ** (1.0 / years)) - 1.0

    cagr = _cagr(float(equity.iloc[-1]) if n else 1.0)
    bh_cagr = _cagr(float(bh_equity.iloc[-1]) if n else 1.0)

    daily_strat = equity.pct_change().dropna()
    sharpe = float(
        np.sqrt(252) * daily_strat.mean() / daily_strat.std()
    ) if not daily_strat.empty and daily_strat.std() > 0 else 0.0

    win_rate = (
        float((trades["return"] > 0).mean()) if not trades.empty else 0.0
    )
    avg_trade = (
        float(trades["return"].mean()) if not trades.empty else 0.0
    )

    return {
        "lookback_days": float(days),
        "bars": float(n),
        "total_return": total_return,
        "buy_hold_return": bh_total_return,
        "cagr": cagr,
        "buy_hold_cagr": bh_cagr,
        "sharpe": sharpe,
        "max_drawdown": _max_drawdown(equity),
        "buy_hold_max_drawdown": _max_drawdown(bh_equity),
        "trades": float(len(trades)),
        "win_rate": win_rate,
        "avg_trade_return": avg_trade,
    }


def _render_chart(
    series: pd.DataFrame,
    trades: pd.DataFrame,
    metrics: dict[str, float],
    tickers: str,
    entry_condition: str,
    exit_condition: str,
    out_path: Path,
) -> None:
    fig = plt.figure(figsize=(10, 8))
    gs = fig.add_gridspec(3, 1, height_ratios=[3, 1, 1.2], hspace=0.4)

    ax_price = fig.add_subplot(gs[0, 0])
    ax_dd = fig.add_subplot(gs[1, 0], sharex=ax_price)
    ax_table = fig.add_subplot(gs[2, 0])
    ax_table.axis("off")

    # Top panel: price + SMA + buy/sell markers; equity overlay on twin axis.
    ax_price.plot(series.index, series["price"], color="#1f77b4", lw=1.0, label=tickers)
    ax_price.plot(
        series.index,
        series["sma_entry"],
        color="#ff7f0e",
        lw=1.0,
        ls="--",
        label=f"SMA {entry_condition.rsplit('_', 1)[-1]}",
    )

    if not trades.empty:
        ax_price.scatter(
            trades["entry_date"], trades["entry_price"],
            marker="^", color="green", s=40, label="Buy", zorder=5,
        )
        ax_price.scatter(
            trades["exit_date"], trades["exit_price"],
            marker="v", color="red", s=40, label="Sell", zorder=5,
        )

    ax_price.set_title(f"{tickers} — Daily Long Backtest")
    ax_price.set_ylabel("Price")
    ax_price.legend(loc="upper left", fontsize=8)
    ax_price.grid(alpha=0.3)

    ax_eq = ax_price.twinx()
    ax_eq.plot(
        series.index, series["equity"],
        color="#2ca02c", lw=1.2, label="Strategy equity",
    )
    ax_eq.plot(
        series.index, series["bh_equity"],
        color="#7f7f7f", lw=1.0, ls=":", label="Buy & hold",
    )
    ax_eq.set_ylabel("Equity (×)")
    ax_eq.legend(loc="upper right", fontsize=8)

    # Middle panel: drawdown.
    ax_dd.fill_between(series.index, series["drawdown"], 0, color="#d62728", alpha=0.3)
    ax_dd.set_ylabel("Drawdown")
    ax_dd.grid(alpha=0.3)
    ax_dd.xaxis.set_major_locator(mdates.YearLocator())
    ax_dd.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))

    # Bottom panel: metrics table.
    rows = [
        ["Lookback (days)", f"{int(metrics['lookback_days'])}"],
        ["Bars", f"{int(metrics['bars'])}"],
        ["Strategy total return", f"{metrics['total_return']:.1%}"],
        ["Buy & hold total return", f"{metrics['buy_hold_return']:.1%}"],
        ["Strategy CAGR", f"{metrics['cagr']:.1%}"],
        ["Buy & hold CAGR", f"{metrics['buy_hold_cagr']:.1%}"],
        ["Sharpe", f"{metrics['sharpe']:.2f}"],
        ["Max drawdown", f"{metrics['max_drawdown']:.1%}"],
        ["Buy & hold max DD", f"{metrics['buy_hold_max_drawdown']:.1%}"],
        ["Trades", f"{int(metrics['trades'])}"],
        ["Win rate", f"{metrics['win_rate']:.1%}"],
        ["Avg trade return", f"{metrics['avg_trade_return']:.2%}"],
    ]
    table = ax_table.table(
        cellText=rows,
        colWidths=[0.5, 0.5],
        cellLoc="left",
        loc="center",
    )
    table.auto_set_font_size(False)
    table.set_fontsize(9)
    table.scale(1.0, 1.2)

    fig.suptitle(
        f"{tickers}  ·  entry={entry_condition}  ·  exit={exit_condition}",
        fontsize=11,
    )
    fig.savefig(out_path, dpi=120, bbox_inches="tight")
    plt.close(fig)


# ---------------------------------------------------------------------------
# Convenience: tool schemas (the same shapes the slide's Claude loop uses)
# ---------------------------------------------------------------------------

QUERY_MARKET_DATA_SCHEMA: dict[str, Any] = {
    "name": "query_market_data",
    "description": (
        "Run a read-only SQL SELECT or WITH query against the `ohlcv` "
        "table (columns: ticker, date, open, high, low, close, volume). "
        "Returns columns + rows. The table stores per-ticker daily bars; "
        "ratio tickers like 'GC=F/HG=F' are NOT stored directly — compute "
        "them with two subqueries."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "sql": {
                "type": "string",
                "description": "A SELECT or WITH SQL query.",
            },
        },
        "required": ["sql"],
    },
}

BACKTEST_SCHEMA: dict[str, Any] = {
    "name": "backtest",
    "description": (
        "Run a long-only daily backtest with SMA-cross entry/exit rules. "
        "Supports ratio tickers via 'A/B' syntax (e.g. 'GC=F/HG=F'). "
        "Conditions are of the form 'price_cross_{above,below}_sma_<N>'. "
        "Generates a chart and returns metrics."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "tickers": {
                "type": "string",
                "description": "Single ticker or ratio (e.g. 'GC=F' or 'GC=F/HG=F').",
            },
            "entry_condition": {
                "type": "string",
                "description": "e.g. 'price_cross_above_sma_200'.",
            },
            "exit_condition": {
                "type": "string",
                "description": "e.g. 'price_cross_below_sma_200'.",
            },
            "days": {
                "type": "integer",
                "description": "Lookback in calendar days.",
                "default": 3650,
            },
            "timeframe": {
                "type": "string",
                "enum": ["daily"],
                "default": "daily",
            },
        },
        "required": ["tickers", "entry_condition", "exit_condition"],
    },
}


__all__ = [
    "DEFAULT_DB_PATH",
    "DEFAULT_TICKERS",
    "BacktestResult",
    "BACKTEST_SCHEMA",
    "QUERY_MARKET_DATA_SCHEMA",
    "backtest",
    "connect",
    "query_market_data",
    "seed_ohlcv",
]
