"""Cost of hedging a long SPY position with listed puts.

Market data: close of Monday 21 September 2026.

Per-contract option quotes are unreachable from this sandbox -- the egress
policy blocks every options host (Nasdaq, Cboe, Yahoo, Barchart, polygon).  So
the vol surface is reconstructed from Cboe *index* data, all of it observed:

  * VIX  (30d)  fixes the variance level at 30 days
  * SKEW (30d)  fixes the slope  at 30 days, via the risk-neutral skewness
  * VIX6M, VIX1Y fix the variance level at the two tenors being priced

Each tenor is a raw-SVI slice in total variance,

    w(k) = a + b * ( rho*k + sqrt(k^2 + s^2) ),   k = ln(K/F),  sigma = sqrt(w/T)

which gives the sqrt-shaped wings a real surface has (a linear-in-k smile blows
the deep wings up and corrupts both calibrations).  rho and s are held at
typical SPX values; a and b are solved per tenor.  The one genuinely assumed
input is SKEW_DECAY -- how fast the skew slope flattens with maturity -- which
is why it carries its own sensitivity band below.

Run:  python3 hedge_cost.py
"""

from datetime import date

import numpy as np
from scipy.optimize import brentq
from scipy.stats import norm

# --------------------------------------------------------------------------
# Market data -- close of 21 Sep 2026.  Sources in README.md.
# --------------------------------------------------------------------------
ASOF = date(2026, 9, 21)
SPOT = 767.48             # SPY last
DIV_YIELD = 0.0100        # trailing 12m distribution, $7.58 / share

# Cboe variance-swap-style indices, by calendar days.  VIX3M is implied from
# the VIX3M/VIX ratio of 1.235 reported for the session; the rest are closes.
VIX_CURVE = {30: 0.1482, 91: 0.1830, 182: 0.2036, 365: 0.2143}
SKEW_INDEX = 148.10       # Cboe SKEW; skewness = (100 - SKEW) / 10

# Treasury par yields, same close (2y 4.74%, 10y 4.96%); the front end is
# interpolated down toward SOFR at 3.85%.
RATE_CURVE = {30: 0.0392, 182: 0.0415, 365: 0.0438}

SVI_RHO = -0.75           # typical SPX correlation parameter
SVI_S = 0.30              # smile curvature scale, quoted per sqrt(year)
SKEW_DECAY = 0.40         # skew slope ~ T^-SKEW_DECAY  <- the assumed input

EXPIRIES = [(date(2027, 3, 19), "6 months"), (date(2027, 9, 17), "1 year")]
PROTECTION = [(1.00, "100% (ATM)"), (0.90, "90% (10% deductible)")]


# --------------------------------------------------------------------------
# Curves
# --------------------------------------------------------------------------
def rate(days):
    xs = sorted(RATE_CURVE)
    return float(np.interp(days, xs, [RATE_CURVE[x] for x in xs]))


def vix_at(days):
    """Variance-index level at `days`, interpolated in total variance."""
    xs = sorted(VIX_CURVE)
    tv = [VIX_CURVE[x] ** 2 * x / 365 for x in xs]
    return float(np.sqrt(np.interp(days, xs, tv) / (days / 365)))


# --------------------------------------------------------------------------
# SVI slice
# --------------------------------------------------------------------------
class Slice:
    def __init__(self, T, r, a, b):
        self.T, self.r, self.a, self.b = T, r, a, b
        self.s = SVI_S * np.sqrt(T)
        self.F = SPOT * np.exp((r - DIV_YIELD) * T)

    def vol(self, k):
        w = self.a + self.b * (SVI_RHO * k + np.sqrt(k ** 2 + self.s ** 2))
        return np.sqrt(np.maximum(w, 1e-8) / self.T)

    def vol_at_strike(self, K):
        return float(self.vol(np.log(K / self.F)))

    @property
    def atm_vol(self):
        return float(self.vol(0.0))

    @property
    def skew_slope(self):
        """beta = -d(sigma)/dk at the forward."""
        return -self.b * SVI_RHO / (2 * self.atm_vol * self.T)

    def put(self, K):
        return bs(SPOT, K, self.T, self.r, DIV_YIELD, self.vol_at_strike(K), "p")


def bs(S, K, T, r, q, vol, kind):
    d1 = (np.log(S / K) + (r - q + 0.5 * vol ** 2) * T) / (vol * np.sqrt(T))
    d2 = d1 - vol * np.sqrt(T)
    if kind == "p":
        return K * np.exp(-r * T) * norm.cdf(-d2) - S * np.exp(-q * T) * norm.cdf(-d1)
    return S * np.exp(-q * T) * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)


def _grid(sl, n=6000):
    ks = np.linspace(-1.5, 1.0, n)
    Ks = sl.F * np.exp(ks)
    vols = sl.vol(ks)
    px = np.where(Ks < sl.F, bs(SPOT, Ks, sl.T, sl.r, DIV_YIELD, vols, "p"),
                  bs(SPOT, Ks, sl.T, sl.r, DIV_YIELD, vols, "c"))
    return ks, Ks, px, vols


def var_strike(sl):
    """Fair variance strike from the log contract -- what VIX measures."""
    ks, Ks, px, _ = _grid(sl)
    return float(np.sqrt(2 * np.exp(sl.r * sl.T) * np.trapezoid(px / Ks, ks) / sl.T))


def bkm_skewness(sl):
    """Risk-neutral skewness of ln(S_T/S_t), Bakshi-Kapadia-Madan (1997)."""
    ks, Ks, _, vols = _grid(sl)
    m = np.log(Ks / SPOT)                       # log-strike measured off spot
    call = Ks >= SPOT
    px = np.where(call, bs(SPOT, Ks, sl.T, sl.r, DIV_YIELD, vols, "c"),
                  bs(SPOT, Ks, sl.T, sl.r, DIV_YIELD, vols, "p"))
    # BKM's call and put weights coincide once both are written in
    # m = ln(K/S): the put-side ln(S/K) terms and the sign in front of the put
    # integral cancel.  One weight function over the OTM strip.
    integ = lambda w: np.trapezoid(w * px / Ks, ks)      # dK/K^2 -> dk/K
    V = integ(2 * (1 - m))
    W = integ(6 * m - 3 * m ** 2)
    X = integ(12 * m ** 2 - 4 * m ** 3)
    er = np.exp(sl.r * sl.T)
    mu = er - 1 - er / 2 * V - er / 6 * W - er / 24 * X
    return float((er * W - 3 * mu * er * V + 2 * mu ** 3) / (er * V - mu ** 2) ** 1.5)


# --------------------------------------------------------------------------
# Calibration
# --------------------------------------------------------------------------
def fit_level(days, b, target_vix):
    """Solve the SVI level `a` so the slice reprices the variance index.

    Returns None when the wings alone already overshoot the index, which is
    how the solvers below learn that a given `b` is infeasible.
    """
    T, r = days / 365, rate(days)
    lo = -b * SVI_S * np.sqrt(T) + 1e-7
    if var_strike(Slice(T, r, lo, b)) > target_vix:
        return None
    f = lambda a: var_strike(Slice(T, r, a, b)) - target_vix
    return Slice(T, r, brentq(f, lo, 2.0, xtol=1e-12), b)


def calibrate_30d():
    """VIX fixes the level, SKEW fixes the slope. Both observed."""
    target = (100 - SKEW_INDEX) / 10

    def resid(b):
        sl = fit_level(30, b, VIX_CURVE[30])
        return -10.0 if sl is None else bkm_skewness(sl) - target

    return fit_level(30, brentq(resid, 1e-6, 0.045, xtol=1e-12), VIX_CURVE[30])


def calibrate_tenor(days, beta30):
    """Level from the variance index; slope from the 30d slope, decayed."""
    target_beta = beta30 * (days / 30) ** (-SKEW_DECAY)

    def resid(b):
        sl = fit_level(days, b, vix_at(days))
        return 10.0 if sl is None else sl.skew_slope - target_beta

    return fit_level(days, brentq(resid, 1e-6, 0.60, xtol=1e-12), vix_at(days))


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------
def main():
    s30 = calibrate_30d()
    beta30 = s30.skew_slope
    print(f"SPY {SPOT:.2f}   as of {ASOF}   div yield {DIV_YIELD:.2%}\n")
    print("30-day calibration -- both inputs observed:")
    print(f"  VIX {VIX_CURVE[30]:.2%} and SKEW {SKEW_INDEX} "
          f"(skewness {(100 - SKEW_INDEX) / 10:.2f})")
    print(f"  -> ATM vol {s30.atm_vol:.2%}, skew slope {beta30:.3f} "
          f"({beta30 * -np.log(0.9) * 100:.1f} vol pts across 90-100%)")
    print(f"  cross-check: observed SPY 30-day IV ~12.7%\n")

    rows = []
    for exp, label in EXPIRIES:
        days = (exp - ASOF).days
        sl = calibrate_tenor(days, beta30)
        print(f"{label} ({exp}, {days}d): r={sl.r:.2%} fwd={sl.F:.2f} "
              f"index={vix_at(days):.2%} ATM={sl.atm_vol:.2%} beta={sl.skew_slope:.3f} "
              f"(90/100 skew {sl.skew_slope * -np.log(0.9) * 100:.1f} pts, "
              f"index-ATM spread {(vix_at(days) - sl.atm_vol) * 100:.1f} pts)")
        for prot, tag in PROTECTION:
            K = SPOT * prot
            px = sl.put(K)
            rows.append((label, tag, K, sl.vol_at_strike(K), px,
                         px / SPOT, px / SPOT / sl.T))
    print()

    hdr = (f"{'tenor':<10}{'protection':<22}{'strike':>9}{'IV':>8}"
           f"{'premium':>10}{'% notional':>12}{'% p.a.':>9}")
    print(hdr, "-" * len(hdr), sep="\n")
    for t, tag, K, v, px, pct, ann in rows:
        print(f"{t:<10}{tag:<22}{K:>9.2f}{v:>8.1%}{px:>10.2f}{pct:>12.2%}{ann:>9.2%}")

    print("\nsensitivity of the 1y cost (% of notional):")
    print(f"{'':<24}{'ATM strike':>12}{'90% strike':>12}")
    days = (EXPIRIES[1][0] - ASOF).days
    base_vix, base_decay = dict(VIX_CURVE), SKEW_DECAY
    # SKEW_DECAY = 0.55 lands the 1y skew near conventional SPX levels
    # (~3.5 vol pts across 90-100%), so that row is the "textbook surface" case.
    for label, dvol, decay in (("index vol -2 pts", -0.02, base_decay),
                               ("conventional 1y skew", 0.0, 0.55),
                               ("base (SKEW-propagated)", 0.0, base_decay),
                               ("skew flattens slowly", 0.0, 0.25),
                               ("index vol +2 pts", +0.02, base_decay)):
        globals()["SKEW_DECAY"] = decay
        globals()["VIX_CURVE"] = {k: v + dvol for k, v in base_vix.items()}
        sl = calibrate_tenor(days, beta30)
        print(f"{label:<24}{sl.put(SPOT) / SPOT:>12.2%}"
              f"{sl.put(0.9 * SPOT) / SPOT:>12.2%}"
              f"   [1y ATM vol {sl.atm_vol:.1%}, "
              f"90/100 skew {sl.skew_slope * -np.log(0.9) * 100:.1f} pts]")
    globals()["SKEW_DECAY"], globals()["VIX_CURVE"] = base_decay, base_vix


if __name__ == "__main__":
    main()
