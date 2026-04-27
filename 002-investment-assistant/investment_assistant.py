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
# futures); the rest are useful for exploration. ^VIX is here so the champion
# signal backtest (SPY + VIX combo) can be exercised end-to-end.
DEFAULT_TICKERS: tuple[str, ...] = (
    "GC=F",  # Gold futures
    "HG=F",  # Copper futures
    "CL=F",  # WTI crude
    "SPY",   # S&P 500 ETF
    "^VIX",  # CBOE volatility index
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
    "^VIX": {"start": 18.0, "drift": 0.0,   "vol": 0.0,   "factor": 0.0},
    "TLT":  {"start": 95.0, "drift": 0.00, "vol": 0.13, "factor": -0.30},
    "DX=F": {"start": 104.0, "drift": 0.00, "vol": 0.07, "factor": -0.25},
}

# Synthetic crisis episodes — relative offsets back from the end of the series,
# expressed in trading days. Each episode is (peak_offset, length, drawdown).
# Used for SPY only; VIX spikes are derived from these episodes automatically.
_SYNTHETIC_CRISES: tuple[tuple[int, int, float], ...] = (
    (4400, 250, 0.55),   # ~2008-09 GFC analogue
    (1450, 30, 0.34),    # ~2020 COVID crash analogue
    (900, 240, 0.27),    # ~2022 bear market analogue
)


def _period_to_days(period: str) -> int:
    period = period.strip().lower()
    m = re.match(r"^(\d+)([dmy])$", period)
    if not m:
        raise ValueError(f"unsupported period {period!r}")
    n, unit = int(m.group(1)), m.group(2)
    return {"d": n, "m": n * 30, "y": n * 365}[unit]


def _build_synthetic_spy(business_days: pd.DatetimeIndex, rng: np.random.Generator) -> np.ndarray:
    """SPY-style series with injected crisis episodes.

    The base path is GBM with realistic equity drift/vol. On top of that we
    inject the crises in ``_SYNTHETIC_CRISES`` — each is a smooth drawdown over
    ``length`` days down to ``-drawdown``, then a partial recovery. This is the
    only reason the champion-signal backtest produces any trades on synthetic
    data; without crises the alert condition never fires.
    """
    n = len(business_days)
    params = _SYNTHETIC_PARAMS["SPY"]
    daily_drift = params["drift"] / 252.0
    daily_vol = params["vol"] / np.sqrt(252.0)

    log_returns = daily_drift + daily_vol * rng.normal(size=n)

    for offset_back, length, drawdown in _SYNTHETIC_CRISES:
        peak_idx = n - offset_back
        if peak_idx < 0 or peak_idx + length >= n:
            continue
        # Smooth drawdown: half-cosine from 0 to -drawdown over `length` days.
        decline = -drawdown * 0.5 * (1 - np.cos(np.linspace(0, np.pi, length)))
        # Convert cumulative drawdown trajectory to per-day log returns.
        decline_returns = np.diff(np.concatenate(([0.0], decline)))
        # Stretch volatility during the crisis (3x baseline) to spike VIX.
        crisis_vol = daily_vol * 3.0 * rng.normal(size=length)
        log_returns[peak_idx:peak_idx + length] = decline_returns + crisis_vol
        # Partial recovery over the next `length` days (reclaim ~60%).
        recovery_end = min(peak_idx + 2 * length, n)
        rec_len = recovery_end - (peak_idx + length)
        if rec_len > 0:
            recovery = drawdown * 0.6 * 0.5 * (1 - np.cos(np.linspace(0, np.pi, rec_len)))
            recovery_returns = np.diff(np.concatenate(([0.0], recovery)))
            log_returns[peak_idx + length:recovery_end] += recovery_returns

    return params["start"] * np.exp(np.cumsum(log_returns))


def _build_synthetic_vix(spy_close: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """VIX-style series — mean-reverting AR(1) on log-VIX, spiked by SPY drops.

    The spike mechanism mirrors real-life behaviour: when SPY has a big down
    day, VIX gaps up and decays slowly. This is what makes the alert
    (VIX > 28 for 2 consecutive days) trigger only during real stress.
    """
    n = len(spy_close)
    spy_returns = np.concatenate(([0.0], np.diff(np.log(spy_close))))
    log_vix_target = np.log(18.0)
    kappa = 0.05  # mean reversion speed
    sigma = 0.06  # daily noise on log-VIX
    log_vix = np.full(n, log_vix_target)
    for t in range(1, n):
        # AR(1) toward log(18) plus a non-linear kick from negative SPY returns.
        shock = -10.0 * min(spy_returns[t], 0.0)  # only negative returns spike VIX
        log_vix[t] = (
            log_vix[t - 1]
            + kappa * (log_vix_target - log_vix[t - 1])
            + sigma * rng.normal()
            + shock
        )
    return np.clip(np.exp(log_vix), 8.0, 100.0)


def _seed_synthetic(
    tickers: tuple[str, ...] | list[str],
    period: str,
    db_path: str | os.PathLike[str],
) -> dict[str, int]:
    """Generate a deterministic OHLCV history for each ticker.

    Returns serve as a reasonable proxy for end-to-end testing — real
    correlations and levels obviously won't match. Each ticker's daily log
    return is ``factor * market_shock + idiosyncratic``, so GC and HG share
    some commodity-like drift. SPY gets injected crisis episodes and VIX is
    derived from SPY so the champion-signal backtest has something to trade.
    """
    days = _period_to_days(period)
    end = pd.Timestamp.today().normalize()
    business_days = pd.bdate_range(end=end, periods=int(days * 252 / 365))
    n = len(business_days)

    rng = np.random.default_rng(seed=42)
    market = rng.normal(loc=0.0, scale=1.0, size=n)

    # SPY and VIX use bespoke generators; cache the SPY close so VIX can
    # reference it.
    spy_close: np.ndarray | None = None

    conn = connect(db_path)
    counts: dict[str, int] = {}
    try:
        # Pre-generate SPY if anything in the request needs it (or for VIX).
        wants_spy = "SPY" in tickers or "^VIX" in tickers
        if wants_spy:
            spy_close = _build_synthetic_spy(business_days, rng)

        for ticker in tickers:
            if ticker == "SPY":
                close = spy_close
                params = _SYNTHETIC_PARAMS["SPY"]
                daily_vol = params["vol"] / np.sqrt(252.0)
            elif ticker == "^VIX":
                close = _build_synthetic_vix(spy_close, rng)
                params = _SYNTHETIC_PARAMS["^VIX"]
                daily_vol = 0.05  # for OHLC fuzz only
            else:
                params = _SYNTHETIC_PARAMS.get(
                    ticker,
                    {"start": 100.0, "drift": 0.05, "vol": 0.20, "factor": 0.50},
                )
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
# Champion signal — the "Research Assistant" slide's optimised SPX strategy
# ---------------------------------------------------------------------------
#
# Rules (verbatim from the slide):
#   Alert        : SPX 42-day ROC < -12% AND VIX > 28 for >= 2 consecutive days
#   Entry        : First close above 5-day MA within 20 days of alert AND
#                  VIX >= 26 at entry
#   Minimum hold : 63 trading days (no exit allowed)
#   Exit         : 12% trailing stop from peak (post-min-hold)
#   Max hold     : 504 trading days (~2 years) safety net
#
# The asymmetric VIX thresholds (28 for alert, 26 for entry) are deliberate —
# the slide notes they "surgically skip 2008" because VIX fell to 25.1 before
# the MA cross fired. Don't unify them.

@dataclass
class ChampionSignalResult:
    series: pd.DataFrame      # date-indexed: asset, vix, roc, ma, alert, armed, in_trade, equity, drawdown
    trades: pd.DataFrame      # one row per round-trip with entry/exit details + objective hit flags
    metrics: dict[str, float]
    state_now: dict[str, Any]
    chart_path: Path
    params: dict[str, Any]


def champion_signal_backtest(
    asset_ticker: str = "SPY",
    vix_ticker: str = "^VIX",
    *,
    roc_window: int = 42,
    roc_threshold: float = -0.12,
    vix_alert_threshold: float = 28.0,
    alert_persistence: int = 2,
    confirmation_window: int = 20,
    ma_window: int = 5,
    vix_entry_threshold: float = 26.0,
    min_hold_days: int = 63,
    trailing_stop: float = 0.12,
    max_hold_days: int = 504,
    target_horizon: int = 126,
    target_returns: tuple[float, ...] = (0.15, 0.20),
    days: int = 365 * 25,
    db_path: str | os.PathLike[str] = DEFAULT_DB_PATH,
    charts_dir: str | os.PathLike[str] = CHARTS_DIR,
    chart_filename: str | None = None,
) -> ChampionSignalResult:
    """Run the optimised SPX panic-recovery strategy from the slide."""

    asset = _load_close_series(asset_ticker, days, db_path)
    vix = _load_close_series(vix_ticker, days, db_path)
    idx = asset.index.intersection(vix.index)
    if idx.empty:
        raise ValueError(
            f"no overlapping dates between {asset_ticker!r} and {vix_ticker!r}"
        )
    asset = asset.loc[idx]
    vix = vix.loc[idx]

    roc = asset.pct_change(roc_window)
    ma = asset.rolling(ma_window, min_periods=ma_window).mean()

    alert_today = (roc < roc_threshold) & (vix > vix_alert_threshold)
    # "for >= 2 consecutive days" — true on day t iff the alert holds on
    # day t AND day t-1.
    alert_persistent = alert_today & alert_today.shift(alert_persistence - 1).fillna(False)

    n = len(asset)
    state = "idle"
    armed_clock = 0
    days_held = 0
    entry_idx: int | None = None
    entry_price = peak_price = np.nan

    in_trade_flag = np.zeros(n, dtype=bool)
    armed_flag = np.zeros(n, dtype=bool)
    alert_flag = alert_persistent.values
    trades: list[dict[str, Any]] = []
    pending_alert_date: pd.Timestamp | None = None

    dates = asset.index
    asset_arr = asset.values
    vix_arr = vix.values
    ma_arr = ma.values

    for i in range(n):
        ts = dates[i]
        spy_t = asset_arr[i]
        vix_t = vix_arr[i]
        ma_t = ma_arr[i]

        if state == "idle":
            if alert_flag[i]:
                state = "armed"
                armed_clock = 0
                pending_alert_date = ts

        elif state == "armed":
            armed_flag[i] = True
            armed_clock += 1
            entry_ok = (
                not np.isnan(ma_t)
                and spy_t > ma_t
                and vix_t >= vix_entry_threshold
            )
            if entry_ok:
                state = "in_trade"
                entry_idx = i
                entry_price = float(spy_t)
                peak_price = entry_price
                days_held = 0
                in_trade_flag[i] = True
            elif armed_clock >= confirmation_window:
                state = "idle"
                pending_alert_date = None

        elif state == "in_trade":
            in_trade_flag[i] = True
            days_held += 1
            peak_price = max(peak_price, float(spy_t))
            stop_active = days_held >= min_hold_days
            stop_hit = stop_active and (spy_t / peak_price - 1.0) <= -trailing_stop
            time_out = days_held >= max_hold_days
            if stop_hit or time_out:
                trade = _close_trade(
                    asset, dates, entry_idx, i,
                    entry_price=entry_price,
                    exit_price=float(spy_t),
                    exit_reason="trailing_stop" if stop_hit else "max_hold",
                    alert_date=pending_alert_date,
                    target_horizon=target_horizon,
                    target_returns=target_returns,
                )
                trades.append(trade)
                state = "idle"
                entry_idx = None
                pending_alert_date = None

    # Open trade at series end? Close it at the last bar so metrics finalise.
    if state == "in_trade" and entry_idx is not None:
        trades.append(
            _close_trade(
                asset, dates, entry_idx, n - 1,
                entry_price=entry_price,
                exit_price=float(asset_arr[-1]),
                exit_reason="open_at_end",
                alert_date=pending_alert_date,
                target_horizon=target_horizon,
                target_returns=target_returns,
            )
        )

    daily_return = asset.pct_change().fillna(0.0)
    position = pd.Series(in_trade_flag, index=dates).astype(float)
    strat_return = daily_return * position.shift(1).fillna(0.0)
    equity = (1.0 + strat_return).cumprod()
    bh_equity = (1.0 + daily_return).cumprod()
    drawdown = equity / equity.cummax() - 1.0

    series = pd.DataFrame(
        {
            "asset": asset,
            "vix": vix,
            "roc": roc,
            "ma": ma,
            "alert": pd.Series(alert_flag, index=dates),
            "armed": pd.Series(armed_flag, index=dates),
            "in_trade": position.astype(bool),
            "equity": equity,
            "bh_equity": bh_equity,
            "drawdown": drawdown,
        }
    )
    trades_df = pd.DataFrame(trades)

    metrics = _champion_metrics(equity, bh_equity, trades_df, target_returns, days)

    state_now = _champion_state_snapshot(
        state=state,
        days_held=days_held,
        entry_idx=entry_idx,
        entry_price=entry_price,
        peak_price=peak_price,
        pending_alert_date=pending_alert_date,
        armed_clock=armed_clock,
        confirmation_window=confirmation_window,
        min_hold_days=min_hold_days,
        max_hold_days=max_hold_days,
        trailing_stop=trailing_stop,
        last_date=dates[-1],
        last_asset=float(asset_arr[-1]),
        last_vix=float(vix_arr[-1]),
        last_roc=float(roc.iloc[-1]) if not np.isnan(roc.iloc[-1]) else None,
        last_ma=float(ma_arr[-1]) if not np.isnan(ma_arr[-1]) else None,
    )

    charts_dir = Path(charts_dir)
    charts_dir.mkdir(parents=True, exist_ok=True)
    chart_path = charts_dir / (chart_filename or f"champion_{asset_ticker.replace('^','')}.png")
    _render_champion_chart(series, trades_df, metrics, asset_ticker, vix_ticker, chart_path)

    params = {
        "asset_ticker": asset_ticker,
        "vix_ticker": vix_ticker,
        "roc_window": roc_window,
        "roc_threshold": roc_threshold,
        "vix_alert_threshold": vix_alert_threshold,
        "alert_persistence": alert_persistence,
        "confirmation_window": confirmation_window,
        "ma_window": ma_window,
        "vix_entry_threshold": vix_entry_threshold,
        "min_hold_days": min_hold_days,
        "trailing_stop": trailing_stop,
        "max_hold_days": max_hold_days,
        "target_horizon": target_horizon,
        "target_returns": list(target_returns),
        "days": days,
    }

    return ChampionSignalResult(
        series=series,
        trades=trades_df,
        metrics=metrics,
        state_now=state_now,
        chart_path=chart_path,
        params=params,
    )


def _close_trade(
    asset: pd.Series,
    dates: pd.DatetimeIndex,
    entry_idx: int,
    exit_idx: int,
    *,
    entry_price: float,
    exit_price: float,
    exit_reason: str,
    alert_date: pd.Timestamp | None,
    target_horizon: int,
    target_returns: tuple[float, ...],
) -> dict[str, Any]:
    """Build a trade record, including hit-rate flags for the objective."""
    horizon_end = min(entry_idx + target_horizon, len(asset) - 1)
    horizon_window = asset.iloc[entry_idx:horizon_end + 1]
    max_in_window = float(horizon_window.max())
    peak_return = max_in_window / entry_price - 1.0
    flags = {
        f"hit_+{int(t * 100)}pct_within_{target_horizon}d": peak_return >= t
        for t in target_returns
    }
    return {
        "alert_date": alert_date,
        "entry_date": dates[entry_idx],
        "entry_price": entry_price,
        "exit_date": dates[exit_idx],
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "holding_days": exit_idx - entry_idx,
        "return": exit_price / entry_price - 1.0,
        "peak_return_in_horizon": peak_return,
        **flags,
    }


def _champion_metrics(
    equity: pd.Series,
    bh_equity: pd.Series,
    trades: pd.DataFrame,
    target_returns: tuple[float, ...],
    days: int,
) -> dict[str, float]:
    base = _compute_metrics(equity, bh_equity, trades, days)
    if trades.empty:
        return {**base, "avg_holding_days": 0.0, "time_in_market": 0.0}

    base["avg_holding_days"] = float(trades["holding_days"].mean())
    base["time_in_market"] = float(trades["holding_days"].sum() / max(len(equity), 1))

    for t in target_returns:
        col = f"hit_+{int(t * 100)}pct_within_{int(trades['holding_days'].max() if not trades.empty else 126)}d"
        # Use the actual column names produced by _close_trade:
        for trade_col in trades.columns:
            if trade_col.startswith(f"hit_+{int(t * 100)}pct_within_"):
                base[trade_col] = float(trades[trade_col].mean())
                break
    return base


def _champion_state_snapshot(
    *,
    state: str,
    days_held: int,
    entry_idx: int | None,
    entry_price: float,
    peak_price: float,
    pending_alert_date: pd.Timestamp | None,
    armed_clock: int,
    confirmation_window: int,
    min_hold_days: int,
    max_hold_days: int,
    trailing_stop: float,
    last_date: pd.Timestamp,
    last_asset: float,
    last_vix: float,
    last_roc: float | None,
    last_ma: float | None,
) -> dict[str, Any]:
    """Snapshot of where the strategy stands as of the last bar.

    This is the bit a "live" caller actually wants: should I do anything today?
    """
    snap: dict[str, Any] = {
        "as_of": last_date.strftime("%Y-%m-%d"),
        "state": state,
        "asset_close": last_asset,
        "vix": last_vix,
        "roc_42d": last_roc,
        "ma_5d": last_ma,
    }
    if state == "armed":
        snap["alert_fired_on"] = (
            pending_alert_date.strftime("%Y-%m-%d") if pending_alert_date else None
        )
        snap["days_remaining_in_window"] = max(confirmation_window - armed_clock, 0)
        snap["entry_check"] = {
            "close_above_ma_5d": last_ma is not None and last_asset > last_ma,
            "vix_at_or_above_26": last_vix >= 26.0,
        }
    elif state == "in_trade":
        snap["entry_price"] = entry_price
        snap["peak_price"] = peak_price
        snap["days_held"] = days_held
        snap["min_hold_remaining"] = max(min_hold_days - days_held, 0)
        snap["max_hold_remaining"] = max(max_hold_days - days_held, 0)
        snap["distance_to_trailing_stop"] = (
            (last_asset / peak_price - 1.0) - (-trailing_stop)
        )
        snap["unrealised_return"] = last_asset / entry_price - 1.0
    elif state == "idle":
        # Surface how close we are to firing an alert.
        snap["alert_check"] = {
            "roc_42d_below_-12pct": last_roc is not None and last_roc < -0.12,
            "vix_above_28": last_vix > 28.0,
        }
    return snap


def champion_signal_state(
    asset_ticker: str = "SPY",
    vix_ticker: str = "^VIX",
    *,
    days: int = 365 * 5,
    db_path: str | os.PathLike[str] = DEFAULT_DB_PATH,
    **kwargs: Any,
) -> dict[str, Any]:
    """Cheap query: replays the strategy on the available history and returns
    just the current state snapshot. For "what would the bot say today" calls.
    """
    result = champion_signal_backtest(
        asset_ticker=asset_ticker,
        vix_ticker=vix_ticker,
        days=days,
        db_path=db_path,
        **kwargs,
    )
    return result.state_now


def _render_champion_chart(
    series: pd.DataFrame,
    trades: pd.DataFrame,
    metrics: dict[str, float],
    asset_ticker: str,
    vix_ticker: str,
    out_path: Path,
) -> None:
    fig = plt.figure(figsize=(11, 10))
    gs = fig.add_gridspec(4, 1, height_ratios=[3, 1.2, 1, 1.5], hspace=0.45)

    ax_price = fig.add_subplot(gs[0, 0])
    ax_vix = fig.add_subplot(gs[1, 0], sharex=ax_price)
    ax_dd = fig.add_subplot(gs[2, 0], sharex=ax_price)
    ax_table = fig.add_subplot(gs[3, 0])
    ax_table.axis("off")

    ax_price.plot(series.index, series["asset"], color="#1f77b4", lw=1.0, label=asset_ticker)
    ax_price.plot(series.index, series["ma"], color="#ff7f0e", lw=0.9, ls="--",
                  label="5d MA", alpha=0.7)

    # Shade armed periods + entries/exits
    if not trades.empty:
        for _, t in trades.iterrows():
            ax_price.axvspan(t["entry_date"], t["exit_date"], color="#2ca02c", alpha=0.10)
        ax_price.scatter(trades["entry_date"], trades["entry_price"],
                         marker="^", color="green", s=55, zorder=5, label="Entry")
        ax_price.scatter(trades["exit_date"], trades["exit_price"],
                         marker="v", color="red", s=55, zorder=5, label="Exit")
    ax_price.set_title(f"{asset_ticker} — Champion signal (panic-recovery)")
    ax_price.set_ylabel("Price")
    ax_price.legend(loc="upper left", fontsize=8)
    ax_price.grid(alpha=0.3)

    ax_eq = ax_price.twinx()
    ax_eq.plot(series.index, series["equity"], color="#2ca02c", lw=1.2, label="Strategy")
    ax_eq.plot(series.index, series["bh_equity"], color="#7f7f7f", lw=1.0, ls=":",
               label="Buy & hold")
    ax_eq.set_ylabel("Equity (×)")
    ax_eq.legend(loc="upper right", fontsize=8)

    # VIX panel with alert threshold
    ax_vix.plot(series.index, series["vix"], color="#9467bd", lw=0.9, label=vix_ticker)
    ax_vix.axhline(28, color="red", ls=":", lw=0.8, alpha=0.7, label="Alert (28)")
    ax_vix.axhline(26, color="orange", ls=":", lw=0.8, alpha=0.7, label="Entry (26)")
    ax_vix.set_ylabel("VIX")
    ax_vix.legend(loc="upper right", fontsize=7)
    ax_vix.grid(alpha=0.3)

    # Drawdown panel
    ax_dd.fill_between(series.index, series["drawdown"], 0, color="#d62728", alpha=0.3)
    ax_dd.set_ylabel("Strategy DD")
    ax_dd.grid(alpha=0.3)
    ax_dd.xaxis.set_major_locator(mdates.YearLocator())
    ax_dd.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))

    rows = [
        ["Bars", f"{int(metrics['bars'])}"],
        ["Trades", f"{int(metrics['trades'])}"],
        ["Avg holding (days)", f"{metrics.get('avg_holding_days', 0):.0f}"],
        ["Time in market", f"{metrics.get('time_in_market', 0):.1%}"],
        ["Strategy total return", f"{metrics['total_return']:.1%}"],
        ["Buy & hold total return", f"{metrics['buy_hold_return']:.1%}"],
        ["Strategy CAGR", f"{metrics['cagr']:.1%}"],
        ["Buy & hold CAGR", f"{metrics['buy_hold_cagr']:.1%}"],
        ["Sharpe", f"{metrics['sharpe']:.2f}"],
        ["Max drawdown", f"{metrics['max_drawdown']:.1%}"],
        ["Buy & hold max DD", f"{metrics['buy_hold_max_drawdown']:.1%}"],
        ["Win rate", f"{metrics['win_rate']:.1%}"],
        ["Avg trade return", f"{metrics['avg_trade_return']:.1%}"],
    ]
    # Add hit-rate rows if present.
    for k, v in metrics.items():
        if k.startswith("hit_+"):
            label = k.replace("_", " ").replace("hit ", "hit-rate ")
            rows.append([label, f"{v:.1%}"])
    table = ax_table.table(cellText=rows, colWidths=[0.55, 0.45], cellLoc="left", loc="center")
    table.auto_set_font_size(False)
    table.set_fontsize(9)
    table.scale(1.0, 1.2)

    fig.suptitle(
        f"{asset_ticker} champion signal · alert: 42d ROC<-12% & VIX>28 (≥2d) · entry: close>5dMA & VIX≥26",
        fontsize=10,
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


CHAMPION_SIGNAL_SCHEMA: dict[str, Any] = {
    "name": "champion_signal_backtest",
    "description": (
        "Run the optimised SPX panic-recovery strategy from the Research "
        "Assistant slide. Alert: <asset> 42d ROC < -12% AND VIX > 28 for "
        "≥2 consecutive days. Entry: first close > 5d MA within 20 days "
        "AND VIX ≥ 26 at entry. Min hold 63d, 12% trailing stop after that, "
        "max hold 504d. Returns equity curve, trade log, metrics, current "
        "state snapshot and chart. All thresholds are overridable for "
        "sensitivity analysis."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "asset_ticker": {"type": "string", "default": "SPY"},
            "vix_ticker": {"type": "string", "default": "^VIX"},
            "roc_window": {"type": "integer", "default": 42},
            "roc_threshold": {"type": "number", "default": -0.12},
            "vix_alert_threshold": {"type": "number", "default": 28.0},
            "alert_persistence": {"type": "integer", "default": 2},
            "confirmation_window": {"type": "integer", "default": 20},
            "ma_window": {"type": "integer", "default": 5},
            "vix_entry_threshold": {"type": "number", "default": 26.0},
            "min_hold_days": {"type": "integer", "default": 63},
            "trailing_stop": {"type": "number", "default": 0.12},
            "max_hold_days": {"type": "integer", "default": 504},
            "target_horizon": {"type": "integer", "default": 126},
            "days": {"type": "integer", "default": 365 * 25},
        },
    },
}


__all__ = [
    "BACKTEST_SCHEMA",
    "CHAMPION_SIGNAL_SCHEMA",
    "DEFAULT_DB_PATH",
    "DEFAULT_TICKERS",
    "QUERY_MARKET_DATA_SCHEMA",
    "BacktestResult",
    "ChampionSignalResult",
    "backtest",
    "champion_signal_backtest",
    "champion_signal_state",
    "connect",
    "query_market_data",
    "seed_ohlcv",
]
