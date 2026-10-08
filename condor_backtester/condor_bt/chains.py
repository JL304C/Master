"""Option chain sources.

Two interchangeable sources, both returning prices in SPX-equivalent points:

- ``SyntheticChain``: Black-76 prices from the parametric vol surface in
  ``pricing.py``, driven by the market CSV (SPX close, VIX, VIX3M, rate).
  Works with free data, but the results are only as good as the surface model.
- ``CsvChain``: real end-of-day put quotes you export from a vendor
  (ThetaData, ORATS, CBOE DataShop, Polygon, ...). This is what decisions
  should be based on.

CsvChain schema (one row per put per day; native product units, i.e. XSP
strikes/prices as XSP shows them, ES/MES in futures points):

    date,expiration,strike,right,bid,ask[,delta][,iv][,underlying]

``right`` must be P/put (calls are ignored). If ``delta`` is missing it is
computed from ``iv``; if ``iv`` is missing too, it is implied from the mid.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

import pandas as pd

from .calendar import Expiration, next_quarterly
from .data import MarketData
from .pricing import VolSurface, implied_vol, put_delta, put_price
from .products import Product


@dataclass(frozen=True)
class Quote:
    strike: float   # SPX-equivalent points
    bid: float
    ask: float
    delta: float

    @property
    def mid(self) -> float:
        return 0.5 * (self.bid + self.ask)


def year_frac(d0: date, d1: date) -> float:
    return max((d1 - d0).days, 0) / 365.0


def forward_price(product: Product, spot: float, d: date, exp: date, r: float, q: float) -> float:
    """Price of the option's underlying, in SPX points.

    Index products: forward to the option expiration.
    Futures products: the quarterly future the option settles against (the
    first quarterly expiring on/after the option), so ES/MES carry basis.
    """
    if product.underlying_kind == "futures":
        t = year_frac(d, next_quarterly(exp))
    else:
        t = year_frac(d, exp)
    return spot * math.exp((r - q) * t)


class SyntheticChain:
    def __init__(self, product: Product, market: MarketData, surface: VolSurface,
                 div_yield: float, strike_range: float = 0.45):
        self.product = product
        self.market = market
        self.surface = surface
        self.q = div_yield
        self.strike_range = strike_range

    def _setup(self, d: date, exp: date):
        row = self.market.row(d)
        t = year_frac(d, exp)
        f = forward_price(self.product, row.close, d, exp, row.rate, self.q)
        atm = self.surface.atm_vol(max(t, 1 / 365.0), row.vix, row.vix3m)
        return row, t, f, atm

    def puts(self, d: date, exp: Expiration) -> list[Quote]:
        row, t, f, atm = self._setup(d, exp.date)
        step = self.product.strike_step
        lo = max(math.floor(f * (1 - self.strike_range) / step) * step, step)
        hi = math.ceil(f * 1.05 / step) * step
        out = []
        k = lo
        while k <= hi:
            vol = self.surface.vol(k, f, t, atm)
            px = put_price(f, k, t, row.rate, vol)
            dl = put_delta(f, k, t, row.rate, self.q, vol, self.product.underlying_kind)
            out.append(Quote(k, px, px, dl))
            k += step
        return out

    def mid(self, d: date, exp: date, strike: float) -> float | None:
        if strike <= 0:
            return 0.0
        row, t, f, atm = self._setup(d, exp)
        return put_price(f, strike, t, row.rate, self.surface.vol(strike, f, t, atm))

    def quote(self, d: date, exp: date, strike: float) -> tuple[float, float] | None:
        m = self.mid(d, exp, strike)
        return None if m is None else (m, m)

    def stressed_value(self, d: date, exp: date, strikes_signs, spot_mult: float, vol_mult: float) -> float:
        """Position value under a spot/vol shock (used by the SPAN-like margin model)."""
        row, t, f, atm = self._setup(d, exp)
        f2, atm2 = f * spot_mult, atm * vol_mult
        return sum(sign * put_price(f2, k, t, row.rate, self.surface.vol(k, f2, t, atm2))
                   for k, sign in strikes_signs if k > 0)


class CsvChain:
    def __init__(self, path: str, product: Product, market: MarketData, div_yield: float):
        self.product = product
        self.market = market
        self.q = div_yield
        df = pd.read_csv(path)
        df.columns = [c.strip().lower() for c in df.columns]
        need = {"date", "expiration", "strike", "right", "bid", "ask"}
        missing = need - set(df.columns)
        if missing:
            raise ValueError(f"chain CSV missing columns: {sorted(missing)}")
        df = df[df["right"].astype(str).str.upper().str[0] == "P"].copy()
        df["date"] = pd.to_datetime(df["date"]).dt.date
        df["expiration"] = pd.to_datetime(df["expiration"]).dt.date
        s = product.price_scale
        df["strike"] = df["strike"].astype(float) / s
        df["bid"] = df["bid"].astype(float) / s
        df["ask"] = df["ask"].astype(float) / s
        if "underlying" in df:
            df["underlying"] = df["underlying"].astype(float) / s
        self._by_key = {key: g.sort_values("strike") for key, g in df.groupby(["date", "expiration"])}
        self._last_mid: dict[tuple[date, float], float] = {}
        # (date, exp, strike) -> (bid, ask) for fast marking
        self._q = {(r.date, r.expiration, r.strike): (r.bid, r.ask)
                   for r in df[["date", "expiration", "strike", "bid", "ask"]].itertuples(index=False)}

    def expirations_on(self, d: date) -> list[date]:
        return sorted(e for (dd, e) in self._by_key if dd == d)

    def puts(self, d: date, exp: Expiration) -> list[Quote]:
        g = self._by_key.get((d, exp.date))
        if g is None:
            return []
        row = self.market.row(d)
        t = year_frac(d, exp.date)
        if "underlying" in g and g["underlying"].notna().any():
            under = float(g["underlying"].dropna().iloc[0])
            f = under if self.product.underlying_kind == "futures" else under * math.exp((row.rate - self.q) * t)
        else:
            f = forward_price(self.product, row.close, d, exp.date, row.rate, self.q)
        out = []
        for r in g.itertuples(index=False):
            if not (r.ask > 0) or r.bid < 0:
                continue
            delta = getattr(r, "delta", math.nan)
            if delta is None or not math.isfinite(delta):
                vol = getattr(r, "iv", math.nan)
                if vol is None or not math.isfinite(vol) or vol <= 0:
                    vol = implied_vol(0.5 * (r.bid + r.ask), f, r.strike, t, row.rate)
                if vol is None:
                    continue
                delta = put_delta(f, r.strike, t, row.rate, self.q, vol, self.product.underlying_kind)
            out.append(Quote(float(r.strike), float(r.bid), float(r.ask), -abs(float(delta))))
        return out

    def quote(self, d: date, exp: date, strike: float) -> tuple[float, float] | None:
        return self._q.get((d, exp, strike))

    def mid(self, d: date, exp: date, strike: float) -> float | None:
        q = self.quote(d, exp, strike)
        if q is not None:
            m = 0.5 * (q[0] + q[1])
            self._last_mid[(exp, strike)] = m
            return m
        return self._last_mid.get((exp, strike))  # carry the last mark over data gaps
