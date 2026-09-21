"""Cost of hedging a long SPY position with listed puts.

Market data as of the close on Friday 18 September 2026.  Live option chains are
not reachable from this environment, so the vol surface is *reconstructed*: a
standard SPX-style skew is laid over the Cboe variance-swap indices (VIX, VIX3M,
VIX6M, VIX1Y), and the ATM level at each tenor is solved for so that the model
surface reprices the corresponding index.  Puts are then priced off that surface
with Black-Scholes.

Run:  python3 hedge_cost.py
"""

import numpy as np
from scipy.stats import norm
from scipy.optimize import brentq

# --------------------------------------------------------------------------
# Market inputs (18 Sep 2026 close)
# --------------------------------------------------------------------------
SPOT = 761.69          # SPY last
DIV_YIELD = 0.0100     # trailing 12m distribution yield ($7.58 / share)

RATES = {0.5: 0.0420, 1.0: 0.0441}          # ~Treasury/OIS to each tenor
VIX_INDEX = {1 / 12: 0.1481, 0.25: 0.1855,  # Cboe variance-swap-style indices
             0.5: 0.2030, 1.0: 0.2172}

# Linear-in-log-moneyness skew, beta = -d(sigma)/d(log K/F), with the usual
# beta ~ T^-0.4 decay anchored at a 1y 90/100 skew of ~3.5 vol points, plus a
# mild wing convexity term.
BETA_1Y = 0.33
SKEW_DECAY = 0.40
CONVEXITY = 0.15
WING_LO, WING_HI = -0.50, 0.35   # flat vol extrapolation beyond the quoted wings


def beta(T):
    return BETA_1Y * T ** (-SKEW_DECAY)


def smile(k, T, atm_vol):
    """Implied vol at log-moneyness k = ln(K/F)."""
    k = np.clip(k, WING_LO, WING_HI)
    return np.clip(atm_vol - beta(T) * k + CONVEXITY * k ** 2, 0.04, 1.50)


# --------------------------------------------------------------------------
# Black-Scholes (spot/forward with continuous dividend yield)
# --------------------------------------------------------------------------
def bs(S, K, T, r, q, vol, kind):
    d1 = (np.log(S / K) + (r - q + 0.5 * vol ** 2) * T) / (vol * np.sqrt(T))
    d2 = d1 - vol * np.sqrt(T)
    if kind == "p":
        return K * np.exp(-r * T) * norm.cdf(-d2) - S * np.exp(-q * T) * norm.cdf(-d1)
    return S * np.exp(-q * T) * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)


def var_strike(T, r, q, atm_vol):
    """Fair variance strike from the log contract: the quantity VIX measures."""
    F = SPOT * np.exp((r - q) * T)
    ks = np.linspace(np.log(0.15), np.log(3.5), 4000)      # ln(K/F) grid
    Ks = F * np.exp(ks)
    vols = smile(ks, T, atm_vol)
    otm = np.where(Ks < F,
                   bs(SPOT, Ks, T, r, q, vols, "p"),
                   bs(SPOT, Ks, T, r, q, vols, "c"))
    integrand = otm / Ks ** 2                              # dK = K dk
    integral = np.trapezoid(integrand * Ks, ks)
    return np.sqrt(2 * np.exp(r * T) * integral / T)


def calibrate_atm(T, r, q, target_index):
    return brentq(lambda v: var_strike(T, r, q, v) - target_index, 0.01, 1.0, xtol=1e-10)


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------
def rate(T):
    return RATES.get(T, np.interp(T, sorted(RATES), [RATES[t] for t in sorted(RATES)]))


def main():
    print(f"SPY spot {SPOT:.2f}   div yield {DIV_YIELD:.2%}\n")

    # sanity check: does the surface reproduce the observed 30d ATM vol (~12.7%)?
    T30, r30 = 1 / 12, 0.039
    print(f"check  30d ATM vol implied by VIX {VIX_INDEX[T30]:.2%}: "
          f"{calibrate_atm(T30, r30, DIV_YIELD, VIX_INDEX[T30]):.2%} "
          f"(observed SPY 30d IV ~12.7%)\n")

    rows = []
    for T, label in ((0.5, "6 months"), (1.0, "1 year")):
        r = rate(T)
        atm = calibrate_atm(T, r, DIV_YIELD, VIX_INDEX[T])
        F = SPOT * np.exp((r - DIV_YIELD) * T)
        print(f"{label}:  r={r:.2%}  forward={F:.2f}  "
              f"VIX{'6M' if T == 0.5 else '1Y'}={VIX_INDEX[T]:.2%}  ATM vol={atm:.2%}")

        for prot, tag in ((1.00, "100% (ATM)"), (0.90, "90% (10% deductible)")):
            K = SPOT * prot
            vol = float(smile(np.log(K / F), T, atm))
            px = bs(SPOT, K, T, r, DIV_YIELD, vol, "p")
            rows.append((label, tag, K, vol, px, px / SPOT, px / SPOT / T))
        print()

    hdr = f"{'tenor':<10}{'protection':<22}{'strike':>9}{'IV':>8}{'premium':>10}{'% notional':>12}{'% p.a.':>9}"
    print(hdr)
    print("-" * len(hdr))
    for t, tag, K, v, px, pct, ann in rows:
        print(f"{t:<10}{tag:<22}{K:>9.2f}{v:>8.1%}{px:>10.2f}{pct:>12.2%}{ann:>9.2%}")

    # sensitivity of the 1y numbers to the two judgement calls
    print("\nsensitivity: 1y cost as % of notional")
    print(f"{'':<16}{'ATM strike':>12}{'90% strike':>12}")
    for dv, vlabel in ((-0.02, "vol -2 pts"), (0.0, "base"), (0.02, "vol +2 pts")):
        for db, blabel in ((-0.10, "skew flat"), (0.0, ""), (0.10, "skew steep")):
            if dv and db:
                continue
            T, r = 1.0, rate(1.0)
            atm = calibrate_atm(T, r, DIV_YIELD, VIX_INDEX[T]) + dv
            F = SPOT * np.exp((r - DIV_YIELD) * T)
            global BETA_1Y
            keep, BETA_1Y = BETA_1Y, BETA_1Y + db
            out = []
            for prot in (1.00, 0.90):
                K = SPOT * prot
                out.append(bs(SPOT, K, T, r, DIV_YIELD,
                              float(smile(np.log(K / F), T, atm)), "p") / SPOT)
            BETA_1Y = keep
            name = (vlabel if dv else "") or blabel or "base"
            print(f"{name:<16}{out[0]:>12.2%}{out[1]:>12.2%}")


if __name__ == "__main__":
    main()
