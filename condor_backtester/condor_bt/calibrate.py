"""Fit the synthetic surface (vix_to_atm, skew_put, curv_put) to real put quotes.

Feed it any real chain CSV (same schema as CsvChain) covering a handful of
dates. It grid-searches the surface parameters that minimize squared IV error
on puts with |delta| between 0.05 and 0.50 and 20-150 DTE, then prints the
values to paste under ``surface:`` in the YAML.
"""
from __future__ import annotations

import itertools
import math

import numpy as np

from .calendar import Expiration
from .chains import CsvChain, forward_price, year_frac
from .data import MarketData
from .pricing import SurfaceParams, VolSurface, implied_vol
from .products import Product


def collect_points(chain: CsvChain, product: Product, market: MarketData, q: float,
                   min_dte: int = 20, max_dte: int = 150):
    pts = []
    for (d, e), g in chain._by_key.items():
        dte = (e - d).days
        if not (min_dte <= dte <= max_dte) or not market.has(d):
            continue
        row = market.row(d)
        if not math.isfinite(row.vix):
            continue
        t = year_frac(d, e)
        f = forward_price(product, row.close, d, e, row.rate, q)
        for qt in chain.puts(d, Expiration(e, False, False)):
            if not (0.05 <= -qt.delta <= 0.50) or qt.bid <= 0:
                continue
            iv = implied_vol(qt.mid, f, qt.strike, t, row.rate)
            if iv:
                pts.append((row.vix, row.vix3m, t, f, qt.strike, iv))
    return pts


def fit(pts, base: SurfaceParams) -> tuple[SurfaceParams, float]:
    best, best_err = base, math.inf
    for a, s, c in itertools.product(np.arange(0.70, 1.051, 0.01), np.arange(0.10, 0.701, 0.02),
                                     (0.0, 0.02, 0.05, 0.08)):
        sp = SurfaceParams(vix_to_atm=float(a), skew_put=float(s), curv_put=float(c),
                           skew_call=base.skew_call, vol_floor_frac=base.vol_floor_frac, vol_cap=base.vol_cap)
        surf = VolSurface(sp)
        err = 0.0
        for vix, vix3m, t, f, k, iv in pts:
            atm = surf.atm_vol(t, vix, vix3m)
            err += (surf.vol(k, f, t, atm) - iv) ** 2
        if err < best_err:
            best, best_err = sp, err
    rmse = math.sqrt(best_err / max(len(pts), 1))
    return best, rmse
