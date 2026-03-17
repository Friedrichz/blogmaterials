"""
IBKR Market Data — Fetching quotes and historical bars.

Demonstrates:
  1. Snapshot quotes (delayed or real-time)
  2. Historical bar data
  3. Streaming market data
"""

import pandas as pd
from ib_insync import IB, Stock, util


def connect(port=7497, client_id=2):
    ib = IB()
    ib.connect("127.0.0.1", port, clientId=client_id)
    return ib


def get_snapshot(ib, symbols):
    """Get current market snapshot for a list of symbols.

    Returns a DataFrame with bid/ask/last prices.
    """
    contracts = [Stock(sym, "SMART", "USD") for sym in symbols]
    ib.qualifyContracts(*contracts)

    tickers = []
    for contract in contracts:
        ticker = ib.reqMktData(contract, snapshot=True)
        tickers.append(ticker)

    # Wait for data to arrive
    ib.sleep(2)

    rows = []
    for ticker in tickers:
        rows.append({
            "symbol": ticker.contract.symbol,
            "bid": ticker.bid,
            "ask": ticker.ask,
            "last": ticker.last,
            "volume": ticker.volume,
        })

    return pd.DataFrame(rows).set_index("symbol")


def get_historical_bars(ib, symbol, duration="1 M", bar_size="1 day"):
    """Fetch historical OHLCV bars for a single symbol.

    Args:
        duration: e.g. "1 M", "6 M", "1 Y"
        bar_size: e.g. "1 day", "1 hour", "5 mins"

    Returns a DataFrame indexed by date.
    """
    contract = Stock(symbol, "SMART", "USD")
    ib.qualifyContracts(contract)

    bars = ib.reqHistoricalData(
        contract,
        endDateTime="",
        durationStr=duration,
        barSizeSetting=bar_size,
        whatToShow="TRADES",
        useRTH=True,
    )

    df = util.df(bars)
    if df is not None and not df.empty:
        df.set_index("date", inplace=True)
    return df


def stream_market_data(ib, symbols, duration_seconds=10):
    """Stream live market data for a set of symbols.

    Prints updates for the given duration, then cancels.
    """
    contracts = [Stock(sym, "SMART", "USD") for sym in symbols]
    ib.qualifyContracts(*contracts)

    for contract in contracts:
        ib.reqMktData(contract)

    print(f"Streaming for {duration_seconds}s...")

    def on_ticker_update(tickers):
        for t in tickers:
            print(f"  {t.contract.symbol}: last={t.last} bid={t.bid} ask={t.ask}")

    ib.pendingTickersEvent += on_ticker_update
    ib.sleep(duration_seconds)
    ib.pendingTickersEvent -= on_ticker_update

    for contract in contracts:
        ib.cancelMktData(contract)


def main():
    ib = connect()
    try:
        # Snapshot quotes
        symbols = ["AAPL", "MSFT", "GOOG", "AMZN", "SPY"]
        print("=== Snapshots ===")
        print(get_snapshot(ib, symbols))

        # Historical bars
        print("\n=== Historical Bars (AAPL, 1 month) ===")
        bars = get_historical_bars(ib, "AAPL", duration="1 M")
        print(bars.tail())

        # Streaming (5 seconds)
        print("\n=== Streaming ===")
        stream_market_data(ib, ["AAPL", "SPY"], duration_seconds=5)

    finally:
        ib.disconnect()
        print("\nDisconnected.")


if __name__ == "__main__":
    main()
