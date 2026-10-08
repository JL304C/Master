"""Black-76 pricing, deltas, a parametric SPX volatility surface, and an
implied-vol solver.

The surface is a *model*. It turns VIX / VIX3M into an at-the-money vol
term structure and adds a put skew that is linear in normalized moneyness:

    z     = ln(K / F) / (atm * sqrt(T))
    sigma = atm * (1 - skew_put * z + curv_put * z^2)   for z < 0
    sigma = atm * (1 - skew_call * z)                    for z >= 0

The defaults (vix_to_atm 0.75, skew_put 0.30) were picked with the surface
sweep on real SPX/VIX history 2020-2025: they give a ~2.1 point average
credit and keep the June-2022 loss that the tastytrade reference backtest
shows. The broken-wing credit and the tail losses are very sensitive to these
two numbers, so treat them as assumptions to calibrate against real chains
(see ``calibrate`` in the CLI), not as data.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

SQRT2 = math.sqrt(2.0)


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / SQRT2))


def _d1(f: float, k: float, t: float, vol: float) -> float:
    return (math.log(f / k) + 0.5 * vol * vol * t) / (vol * math.sqrt(t))


def put_price(f: float, k: float, t: float, r: float, vol: float) -> float:
    """Black-76 put on forward/futures price ``f``, discounted at ``r`` over ``t``."""
    if t <= 0 or vol <= 0:
        return max(k - f, 0.0) * (math.exp(-r * t) if t > 0 else 1.0)
    d1 = _d1(f, k, t, vol)
    d2 = d1 - vol * math.sqrt(t)
    return math.exp(-r * t) * (k * norm_cdf(-d2) - f * norm_cdf(-d1))


def put_delta(f: float, k: float, t: float, r: float, q: float, vol: float,
              underlying_kind: str) -> float:
    """Put delta as a broker shows it: vs. the spot index for SPX/XSP,
    vs. the futures price for ES/MES. Negative number."""
    if t <= 0 or vol <= 0:
        return -1.0 if k > f else 0.0
    nd1 = norm_cdf(_d1(f, k, t, vol)) - 1.0
    if underlying_kind == "futures":
        return math.exp(-r * t) * nd1
    return math.exp(-q * t) * nd1


def implied_vol(price: float, f: float, k: float, t: float, r: float) -> float | None:
    """Bisection IV for a put. Returns None when the price is below intrinsic."""
    intrinsic = max(k - f, 0.0) * math.exp(-r * t)
    if t <= 0 or price <= intrinsic + 1e-10:
        return None
    lo, hi = 1e-4, 5.0
    if put_price(f, k, t, r, hi) < price:
        return None
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if put_price(f, k, t, r, mid) > price:
            hi = mid
        else:
            lo = mid
        if hi - lo < 1e-7:
            break
    return 0.5 * (lo + hi)


@dataclass(frozen=True)
class SurfaceParams:
    vix_to_atm: float = 0.75     # 30d ATM IV ~= VIX * this (VIX includes the skew premium)
    skew_put: float = 0.30
    curv_put: float = 0.0
    skew_call: float = 0.15
    vol_floor_frac: float = 0.5  # sigma never below this fraction of ATM
    vol_cap: float = 3.0


class VolSurface:
    def __init__(self, params: SurfaceParams):
        self.p = params

    def atm_vol(self, t_years: float, vix: float, vix3m: float | None) -> float:
        """ATM vol for maturity t from VIX (30d) and optionally VIX3M (93d),
        interpolating total variance; flat outside [30d, 93d]."""
        v30 = vix / 100.0 * self.p.vix_to_atm
        if vix3m is None or not math.isfinite(vix3m):
            return v30
        v93 = vix3m / 100.0 * self.p.vix_to_atm
        t30, t93 = 30 / 365.0, 93 / 365.0
        if t_years <= t30:
            return v30
        if t_years >= t93:
            return v93
        w30, w93 = v30 * v30 * t30, v93 * v93 * t93
        w = w30 + (w93 - w30) * (t_years - t30) / (t93 - t30)
        return math.sqrt(max(w, 1e-12) / t_years)

    def vol(self, k: float, f: float, t_years: float, atm: float) -> float:
        if t_years <= 0:
            return atm
        z = math.log(k / f) / (atm * math.sqrt(t_years))
        if z < 0:
            mult = 1.0 - self.p.skew_put * z + self.p.curv_put * z * z
        else:
            mult = 1.0 - self.p.skew_call * z
        return min(max(atm * mult, atm * self.p.vol_floor_frac), self.p.vol_cap)
