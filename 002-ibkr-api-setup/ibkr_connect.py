"""
IBKR API Connection — Getting started with ib_insync.

This script demonstrates:
  1. Connecting to TWS / IB Gateway
  2. Fetching account summary
  3. Listing current positions

Prerequisites:
  - TWS or IB Gateway running with API enabled
  - Paper trading port 7497 (TWS) or 4002 (IB Gateway)
"""

from ib_insync import IB


def connect(host="127.0.0.1", port=7497, client_id=1):
    """Connect to TWS/IB Gateway and return the IB instance.

    Args:
        host: TWS/Gateway host (default localhost).
        port: 7497 = TWS paper, 7496 = TWS live,
              4002 = Gateway paper, 4001 = Gateway live.
        client_id: Unique ID for this connection (0-31).
    """
    ib = IB()
    ib.connect(host, port, clientId=client_id)
    print(f"Connected — server version: {ib.client.serverVersion()}")
    return ib


def show_account_summary(ib):
    """Print key account metrics."""
    summary = ib.accountSummary()
    keys_of_interest = [
        "NetLiquidation",
        "TotalCashValue",
        "BuyingPower",
        "GrossPositionValue",
        "UnrealizedPnL",
    ]
    print("\n=== Account Summary ===")
    for item in summary:
        if item.tag in keys_of_interest:
            print(f"  {item.tag}: {item.value} {item.currency}")


def show_positions(ib):
    """Print current portfolio positions."""
    positions = ib.positions()
    if not positions:
        print("\nNo open positions.")
        return
    print(f"\n=== Positions ({len(positions)}) ===")
    for pos in positions:
        c = pos.contract
        print(f"  {c.symbol:6s}  qty={pos.position:>8}  "
              f"avg_cost={pos.avgCost:>10.2f}")


def main():
    ib = connect()
    try:
        show_account_summary(ib)
        show_positions(ib)
    finally:
        ib.disconnect()
        print("\nDisconnected.")


if __name__ == "__main__":
    main()
