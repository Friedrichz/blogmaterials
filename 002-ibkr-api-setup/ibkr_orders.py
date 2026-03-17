"""
IBKR Order Placement — Placing and managing orders.

Demonstrates:
  1. Market and limit orders
  2. Bracket orders (entry + take-profit + stop-loss)
  3. Portfolio-based order generation from target weights

IMPORTANT: Use paper trading (port 7497) for testing!
"""

import pandas as pd
from ib_insync import IB, Stock, MarketOrder, LimitOrder


def connect(port=7497, client_id=3):
    ib = IB()
    ib.connect("127.0.0.1", port, clientId=client_id)
    return ib


def place_market_order(ib, symbol, quantity):
    """Place a simple market order.

    Args:
        quantity: Positive for buy, negative for sell.
    """
    contract = Stock(symbol, "SMART", "USD")
    ib.qualifyContracts(contract)

    action = "BUY" if quantity > 0 else "SELL"
    order = MarketOrder(action, abs(quantity))
    trade = ib.placeOrder(contract, order)
    print(f"Market order: {action} {abs(quantity)} {symbol} — "
          f"status: {trade.orderStatus.status}")
    return trade


def place_limit_order(ib, symbol, quantity, limit_price):
    """Place a limit order.

    Args:
        quantity: Positive for buy, negative for sell.
        limit_price: The limit price.
    """
    contract = Stock(symbol, "SMART", "USD")
    ib.qualifyContracts(contract)

    action = "BUY" if quantity > 0 else "SELL"
    order = LimitOrder(action, abs(quantity), limit_price)
    trade = ib.placeOrder(contract, order)
    print(f"Limit order: {action} {abs(quantity)} {symbol} @ {limit_price} — "
          f"status: {trade.orderStatus.status}")
    return trade


def place_bracket_order(ib, symbol, quantity, entry_price, take_profit, stop_loss):
    """Place a bracket order (entry + take-profit + stop-loss).

    All three orders are linked — filling the entry activates TP and SL.
    """
    contract = Stock(symbol, "SMART", "USD")
    ib.qualifyContracts(contract)

    action = "BUY" if quantity > 0 else "SELL"
    bracket = ib.bracketOrder(
        action, abs(quantity), entry_price, take_profit, stop_loss,
    )
    for order in bracket:
        ib.placeOrder(contract, order)

    print(f"Bracket order: {action} {abs(quantity)} {symbol} "
          f"entry={entry_price} tp={take_profit} sl={stop_loss}")
    return bracket


def generate_rebalance_orders(ib, target_weights, cash):
    """Generate orders to match target ETF replicator weights.

    Args:
        target_weights: dict of {symbol: weight_pct} from ETF replicator.
        cash: Total cash to allocate.

    Returns a DataFrame of proposed orders (does NOT execute them).
    """
    symbols = list(target_weights.keys())
    contracts = [Stock(sym, "SMART", "USD") for sym in symbols]
    ib.qualifyContracts(*contracts)

    # Get current prices
    tickers = []
    for contract in contracts:
        tickers.append(ib.reqMktData(contract, snapshot=True))
    ib.sleep(2)

    # Get current positions
    positions = {p.contract.symbol: p.position for p in ib.positions()}

    orders = []
    for ticker in tickers:
        sym = ticker.contract.symbol
        price = ticker.last if ticker.last > 0 else ticker.close
        if price <= 0:
            continue

        target_dollars = cash * target_weights[sym] / 100
        target_shares = int(target_dollars // price)
        current_shares = int(positions.get(sym, 0))
        delta = target_shares - current_shares

        if delta != 0:
            orders.append({
                "symbol": sym,
                "action": "BUY" if delta > 0 else "SELL",
                "quantity": abs(delta),
                "price": price,
                "target_shares": target_shares,
                "current_shares": current_shares,
            })

    for contract in contracts:
        ib.cancelMktData(contract)

    return pd.DataFrame(orders)


def execute_rebalance(ib, orders_df):
    """Execute a set of rebalance orders (market orders).

    Args:
        orders_df: DataFrame from generate_rebalance_orders().
    """
    trades = []
    for _, row in orders_df.iterrows():
        qty = row["quantity"] if row["action"] == "BUY" else -row["quantity"]
        trade = place_market_order(ib, row["symbol"], qty)
        trades.append(trade)
    return trades


def main():
    ib = connect()
    try:
        # Example: generate rebalance orders for a small portfolio
        # These weights would come from the ETF replicator
        sample_weights = {
            "AAPL": 4.37,
            "MSFT": 3.22,
            "AMZN": 2.10,
            "GOOG": 1.80,
            "JPM": 1.13,
        }
        cash = 10_000

        print("=== Proposed Rebalance Orders ===")
        orders = generate_rebalance_orders(ib, sample_weights, cash)
        print(orders.to_string(index=False))

        print("\n(Orders are NOT executed — remove the safety check below to trade)")
        # Uncomment to actually execute:
        # trades = execute_rebalance(ib, orders)

    finally:
        ib.disconnect()
        print("\nDisconnected.")


if __name__ == "__main__":
    main()
