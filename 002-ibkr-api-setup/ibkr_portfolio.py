"""
IBKR Portfolio Sync — Bridge between ETF replicator and IBKR.

This module connects the ETF replicator output (weights/shares) to
the IBKR API for actual portfolio construction and rebalancing.
"""

import pandas as pd
from ib_insync import IB, Stock

from ibkr_connect import connect
from ibkr_market_data import get_snapshot
from ibkr_orders import generate_rebalance_orders, execute_rebalance


def get_current_portfolio(ib):
    """Fetch current IBKR portfolio as a DataFrame."""
    portfolio = ib.portfolio()
    if not portfolio:
        return pd.DataFrame(columns=[
            "symbol", "shares", "market_price", "market_value",
            "avg_cost", "unrealized_pnl",
        ])

    rows = []
    for item in portfolio:
        rows.append({
            "symbol": item.contract.symbol,
            "shares": item.position,
            "market_price": item.marketPrice,
            "market_value": item.marketValue,
            "avg_cost": item.averageCost,
            "unrealized_pnl": item.unrealizedPNL,
        })
    return pd.DataFrame(rows).set_index("symbol")


def compare_portfolios(ib, target_weights, cash):
    """Compare current IBKR portfolio vs target ETF replicator weights.

    Returns a DataFrame showing current vs target allocations.
    """
    current = get_current_portfolio(ib)
    symbols = list(target_weights.keys())

    # Get prices for target symbols
    snapshot = get_snapshot(ib, symbols)

    comparison = []
    for sym in symbols:
        price = snapshot.loc[sym, "last"] if sym in snapshot.index else 0
        target_dollars = cash * target_weights[sym] / 100
        target_shares = int(target_dollars // price) if price > 0 else 0

        current_shares = current.loc[sym, "shares"] if sym in current.index else 0
        current_value = current.loc[sym, "market_value"] if sym in current.index else 0

        comparison.append({
            "symbol": sym,
            "target_weight_pct": target_weights[sym],
            "target_shares": target_shares,
            "current_shares": int(current_shares),
            "delta_shares": target_shares - int(current_shares),
            "target_value": target_dollars,
            "current_value": current_value,
        })

    return pd.DataFrame(comparison).set_index("symbol")


def rebalance(ib, target_weights, cash, dry_run=True):
    """Rebalance IBKR portfolio to match ETF replicator target weights.

    Args:
        target_weights: dict of {symbol: weight_pct}.
        cash: Total portfolio value to target.
        dry_run: If True (default), only print proposed orders without executing.
    """
    print("=== Portfolio Comparison ===")
    comp = compare_portfolios(ib, target_weights, cash)
    print(comp.to_string())

    orders = generate_rebalance_orders(ib, target_weights, cash)

    if orders.empty:
        print("\nPortfolio is already in sync — no orders needed.")
        return

    print(f"\n=== Proposed Orders ({len(orders)}) ===")
    print(orders.to_string(index=False))

    if dry_run:
        print("\n[DRY RUN] No orders executed. Set dry_run=False to trade.")
    else:
        print("\nExecuting orders...")
        trades = execute_rebalance(ib, orders)
        print(f"Placed {len(trades)} orders.")


def main():
    # Example: sync with ETF replicator weights for a $10K portfolio
    # In practice, these weights come from the ETF replicator notebook
    sample_weights = {
        "AAPL": 4.37,
        "MSFT": 3.22,
        "AMZN": 2.10,
        "GOOG": 1.80,
        "JPM": 1.13,
        "JNJ": 1.27,
        "V": 1.10,
        "PG": 0.79,
        "XOM": 1.38,
        "BAC": 0.92,
    }
    cash = 10_000

    ib = connect()
    try:
        rebalance(ib, sample_weights, cash, dry_run=True)
    finally:
        ib.disconnect()
        print("\nDisconnected.")


if __name__ == "__main__":
    main()
