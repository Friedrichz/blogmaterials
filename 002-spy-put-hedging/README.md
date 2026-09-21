# What does it cost to hedge SPY with puts?

Cost of buying listed SPY puts against a long SPY position, at two tenors
(6 months, 1 year) and two protection levels (strike at 100% of spot, and at
90% of spot — i.e. self-insuring the first 10% of the drawdown).

## Market inputs — close of Friday 18 September 2026

| input | value |
|---|---|
| SPY spot | 761.69 |
| SPY distribution yield | 1.00% ($7.58 / yr) |
| Treasury 1y / 6m (approx) | 4.41% / 4.20% |
| VIX / VIX3M / VIX6M / VIX1Y | 14.81 / 18.55 / 20.30 / 21.72 |

## Method

Live option chains are not reachable from the sandbox this was built in, so the
vol surface is reconstructed rather than quoted:

1. A standard SPX-style skew is assumed — implied vol linear in log-moneyness,
   slope decaying as `T^-0.4`, anchored at a 1y 90/100 skew of ~3.7 vol points,
   flat extrapolation beyond the wings.
2. The ATM level at each tenor is solved so the model surface reprices the
   corresponding Cboe variance-swap index (VIX6M, VIX1Y) through the log
   contract.
3. Puts are priced off that surface with Black-Scholes.

Sanity check: calibrating the same way at 30 days against VIX = 14.81 returns an
ATM vol of **12.5%**, against an observed SPY 30-day IV of ~12.7%. Calibrated
ATM vols are 15.9% (6m) and 16.9% (1y).

## Result

| tenor | protection | strike | IV | premium | % of notional | % p.a. |
|---|---|---|---|---|---|---|
| 6 months | 100% (ATM) | 761.69 | 16.6% | $29.48 | **3.87%** | 7.74% |
| 6 months | 90% (10% deductible) | 685.52 | 21.4% | $12.90 | **1.69%** | 3.39% |
| 1 year | 100% (ATM) | 761.69 | 18.0% | $41.47 | **5.44%** | 5.44% |
| 1 year | 90% (10% deductible) | 685.52 | 21.7% | $24.03 | **3.16%** | 3.16% |

Sensitivity of the 1-year numbers (% of notional):

| scenario | ATM strike | 90% strike |
|---|---|---|
| vol −2 pts | 4.69% | 2.57% |
| skew flat | 5.31% | 2.75% |
| base | 5.44% | 3.16% |
| skew steep | 5.57% | 3.58% |
| vol +2 pts | 6.21% | 3.76% |

## Notes

- **Giving up the first 10% cuts the bill by ~40–55%.** The ATM put spends most
  of its premium insuring small moves that never threaten the position.
- **Short tenors are worse per year of cover.** The 6-month ATM hedge annualises
  at 7.7% versus 5.4% for the 1-year. Rolling 6-month puts twice is not the same
  trade as one 1-year put, and costs materially more in premium (the compensation
  is a strike that resets to the new spot at the roll).
- Part of the ATM premium is carry, not insurance: the 1-year forward is 788, so
  a strike at spot (761.69) is already ~3.4% below the forward.
- Vol is the dominant uncertainty. These are model prices off a reconstructed
  surface — treat them as ±0.5 point on the % of notional, not as executable
  quotes. Bid/ask and the fact that a $760 strike, not 761.69, is the listed
  one will move the realised cost.

## Run

```
pip install numpy scipy
python3 hedge_cost.py
```

## Sources

SPY and VIX-complex levels, Treasury yields and SPY distribution yield were
taken from public market data for 18 September 2026 (Cboe index dashboard,
Treasury daily par yield curve, stockanalysis.com).
