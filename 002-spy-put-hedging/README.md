# What does it cost to hedge SPY with puts?

Cost of buying SPY puts against a long SPY position, at two tenors (6 months,
1 year) and two protection levels (strike at 100% of spot, and at 90% — i.e.
self-insuring the first 10% of the drawdown).

## Market data — close of Monday 21 September 2026

All observed, not assumed:

| input | value | source |
|---|---|---|
| SPY last | 767.48 | close, 21 Sep 2026 |
| SPY distribution yield | 1.00% ($7.58 / share) | trailing 12m |
| VIX | 14.82 | Cboe |
| VIX3M (implied from the 1.235 VIX3M/VIX ratio) | 18.30 | Cboe |
| VIX6M | 20.36 | Cboe |
| VIX1Y | 21.43 | Cboe |
| Cboe SKEW | 148.10 | Cboe |
| Treasury 2y / 10y | 4.74% / 4.96% | close, 21 Sep 2026 |
| SOFR | 3.85% | overnight |

## What is *not* real data

Per-contract option quotes. Every options host — Nasdaq, Cboe, Yahoo, Barchart,
Market Chameleon, polygon.io — is refused at the CONNECT stage by this
environment's egress policy (HTTP 403). There is no chain to read, so the
surface is reconstructed from index data instead.

## Method

Each tenor is a raw-SVI slice in total variance,

```
w(k) = a + b * ( rho*k + sqrt(k^2 + s^2) )     k = ln(K/F),  sigma = sqrt(w/T)
```

`rho = -0.75` and `s = 0.30*sqrt(T)` are held at typical SPX values; `a` and `b`
are solved per tenor:

1. **30 days** — solved jointly against two observables. VIX fixes the variance
   level (through the log contract); SKEW fixes the slope (through the
   Bakshi–Kapadia–Madan risk-neutral skewness, `skewness = (100 − SKEW)/10 = −4.81`).
   Result: ATM vol 10.9%, skew of 13.3 vol points across 90–100%.
2. **6m / 1y** — level from VIX6M / VIX1Y, interpolated in total variance to the
   actual expiry (19 Mar 2027, 179d; 17 Sep 2027, 361d). Slope from the 30-day
   slope decayed as `T^-0.4`.
3. Puts priced off the fitted slice with Black–Scholes.

SVI matters here: a linear-in-log-moneyness smile sends the deep wings to
absurd vols, which corrupts both the variance and the skewness integral. SVI's
sqrt-shaped wings are what a real surface does.

The strike strip used for the skewness integral turns out not to matter —
truncating it from ±1.5 in log-moneyness down to [−0.5, +0.2] moves the fitted
skew from 13.3 to 13.9 vol points.

## Result

| tenor | protection | strike | IV | premium | % of notional | % p.a. |
|---|---|---|---|---|---|---|
| 6 months | 100% (ATM) | 767.48 | 13.8% | $23.69 | **3.09%** | 6.29% |
| 6 months | 90% (10% deductible) | 690.73 | 20.6% | $11.63 | **1.52%** | 3.09% |
| 1 year | 100% (ATM) | 767.48 | 14.4% | $31.39 | **4.09%** | 4.14% |
| 1 year | 90% (10% deductible) | 690.73 | 19.5% | $19.09 | **2.49%** | 2.51% |

### The honest range

The one assumed input — how fast skew flattens with maturity — is also the one
that moves the answer most, because the variance index is pinned: a steeper
long-dated skew forces a lower ATM vol out of the same VIX1Y, which makes the
ATM put cheaper.

| | 6m ATM | 6m 90% | 1y ATM | 1y 90% |
|---|---|---|---|---|
| base, 30-day SKEW propagated at `T^-0.4` | 3.09% | 1.52% | 4.09% | 2.49% |
| conventional SPX skew (`T^-0.55`) | 3.54% | 1.60% | 4.92% | 2.77% |

**The conventional case is the more plausible one.** The base case implies a
1-year ATM vol of 12.8% against a VIX1Y of 21.4 — an index-to-ATM spread of 8.6
vol points, where SPX historically runs 2–4. That is the model telling you the
30-day SKEW reading, propagated out a year, over-steepens the long end.

So read the answer as a range: **1-year ATM protection costs roughly 4–5% of
notional, 1-year 90% protection roughly 2.5–2.8%, 6-month ATM roughly 3.1–3.5%,
6-month 90% roughly 1.5–1.6%.**

## Notes

- **Giving up the first 10% cuts the bill in half.** 2.49% versus 4.09% at one
  year, 1.52% versus 3.09% at six months. The ATM put spends most of its premium
  insuring small moves that never threaten the position.
- **Short tenors cost more per year of cover.** The 6-month ATM hedge annualises
  at 6.3% against 4.1% for the 1-year. Rolling 6-month puts twice is not the same
  trade as one 1-year put — you pay materially more, and what you buy is a strike
  that resets to the new spot at each roll.
- **Part of the ATM premium is carry, not insurance.** The 1-year forward is
  793.53, so a strike at spot is already 3.3% below the forward.
- This was a "cheap at the money, dear in the tail" session — VIX at the lows
  with SKEW at 148 — so ATM protection is unusually cheap relative to the tail,
  and the 90% strike unusually dear relative to ATM. Both numbers are
  regime-dependent.
- Listed SPY strikes at these expiries come in 5-point increments, so the
  tradeable tickets are the 765/770 and the 690, not 767.48 and 690.73.
- Bid/ask is not modelled. On 1-year SPY puts it is real money.

## Run

```
pip install numpy scipy
python3 hedge_cost.py
```
