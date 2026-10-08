"""Market data loader.

Expected CSV (``scripts/fetch_market_data.py`` writes exactly this):

    date,open,close,vix,vix3m,rate
    2020-10-01,3385.87,3380.80,26.70,30.50,0.0010

- ``open``/``close``: SPX index. ``open`` is used as the AM settlement proxy
  (SOQ); if it is missing, AM settlement falls back to the close.
- ``vix``: required for the synthetic chain. ``vix3m`` optional (term structure).
- ``rate``: annualized decimal (0.05 = 5%). Optional; config ``risk_free_rate``
  fills gaps.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

import pandas as pd


@dataclass(frozen=True)
class MarketRow:
    d: date
    open: float
    close: float
    vix: float
    vix3m: float
    rate: float


class MarketData:
    def __init__(self, df: pd.DataFrame, default_rate: float):
        df = df.copy()
        df.columns = [c.strip().lower() for c in df.columns]
        if "date" not in df or "close" not in df:
            raise ValueError("market CSV needs at least 'date' and 'close' columns")
        df["date"] = pd.to_datetime(df["date"]).dt.date
        df = df.sort_values("date").drop_duplicates("date", keep="last")
        for col in ("open", "vix", "vix3m", "rate"):
            if col not in df:
                df[col] = math.nan
        df["rate"] = df["rate"].astype(float).ffill().fillna(default_rate)
        df["vix"] = df["vix"].astype(float).ffill()
        df["vix3m"] = df["vix3m"].astype(float).ffill()
        df = df.dropna(subset=["close"])
        self.df = df.set_index("date")
        self._rows = {
            d: MarketRow(d, float(r.open), float(r.close), float(r.vix), float(r.vix3m), float(r.rate))
            for d, r in self.df.iterrows()
        }
        self.dates: list[date] = list(self.df.index)

    @classmethod
    def from_csv(cls, path: str, default_rate: float = 0.04) -> "MarketData":
        return cls(pd.read_csv(path), default_rate)

    def row(self, d: date) -> MarketRow:
        return self._rows[d]

    def has(self, d: date) -> bool:
        return d in self._rows

    def slice(self, start: date, end: date) -> "MarketData":
        sub = self.df.loc[(self.df.index >= start) & (self.df.index <= end)].reset_index()
        return MarketData(sub, default_rate=float(self.df["rate"].iloc[0]))
