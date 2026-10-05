"""Option math for the backtest: implied forward/discount from put-call parity,
Black-76 implied vol and delta.

Databento's OPRA data has quotes but no greeks, so delta is computed here.
Each expiry's forward F and discount factor DF are backed out of the chain
itself (C - P = DF*F - DF*K, fit across near-the-money strikes), so no
external interest-rate or dividend assumptions are needed. SPY options are
American, but early-exercise value on a ~10-delta put is negligible, so the
European Black-76 model is used.
"""
import math

import numpy as np
from scipy.special import ndtr as norm_cdf


def fit_forward(strikes, call_mid, put_mid, ref_price=None, band=0.05, min_points=3):
    """Least-squares fit of C - P = a + b*K, giving DF = -b and F = a/DF.

    Uses strikes within +/-band of ref_price (or of the median strike if no
    ref_price). Returns (F, DF) or (None, None) if the fit is unusable.
    """
    K = np.asarray(strikes, dtype=float)
    y = np.asarray(call_mid, dtype=float) - np.asarray(put_mid, dtype=float)
    ok = np.isfinite(K) & np.isfinite(y)
    K, y = K[ok], y[ok]
    if len(K) < min_points:
        return None, None
    center = ref_price if ref_price else K[np.argmin(np.abs(y))]
    sel = np.abs(K / center - 1.0) <= band
    if sel.sum() < min_points:
        # widen to the min_points strikes nearest the center
        sel = np.zeros_like(K, dtype=bool)
        sel[np.argsort(np.abs(K - center))[:min_points]] = True
    b, a = np.polyfit(K[sel], y[sel], 1)
    df = -b
    if not (0.85 < df <= 1.0005):
        return None, None
    df = min(df, 1.0)
    return a / df, df


def black76_put(F, K, T, DF, sigma):
    F, K, sigma = np.asarray(F, float), np.asarray(K, float), np.asarray(sigma, float)
    vol_t = sigma * math.sqrt(T)
    d1 = (np.log(F / K) + 0.5 * vol_t ** 2) / vol_t
    d2 = d1 - vol_t
    return DF * (K * norm_cdf(-d2) - F * norm_cdf(-d1))


def implied_vol_put(price, F, K, T, DF, lo=0.005, hi=5.0, iters=80):
    """Vectorized bisection. NaN where price is outside the no-arbitrage range."""
    price = np.asarray(price, float)
    K = np.asarray(K, float)
    intrinsic = DF * np.maximum(K - F, 0.0)
    upper = DF * K
    valid = (price > intrinsic) & (price < upper) & np.isfinite(price)
    lo_v = np.full(price.shape, lo)
    hi_v = np.full(price.shape, hi)
    for _ in range(iters):
        mid = 0.5 * (lo_v + hi_v)
        too_high = black76_put(F, K, T, DF, mid) > price
        hi_v = np.where(too_high, mid, hi_v)
        lo_v = np.where(too_high, lo_v, mid)
    iv = 0.5 * (lo_v + hi_v)
    return np.where(valid, iv, np.nan)


def put_delta(F, K, T, sigma, spot_factor=1.0):
    """Spot delta of a put: -e^{-qT} N(-d1). spot_factor = e^{-qT} = DF*F/S."""
    vol_t = np.asarray(sigma, float) * math.sqrt(T)
    d1 = (np.log(F / np.asarray(K, float)) + 0.5 * vol_t ** 2) / vol_t
    return -spot_factor * norm_cdf(-d1)


def regt_naked_put_bp(spot, strike, premium):
    """Reg T naked put requirement in dollars per contract:
    max(20% of underlying - OTM amount + premium, 10% of strike + premium) * 100."""
    otm = max(spot - strike, 0.0)
    return max(0.20 * spot - otm + premium, 0.10 * strike + premium) * 100.0
