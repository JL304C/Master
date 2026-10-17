"""What is known about a symbol before the open on a given day (prior daily bars only)."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import pandas as pd

from data.bars import adjust_daily, daily_bars
from indicators.core import atr, sma


class NoHistory(ValueError):
    """Not enough daily bars before the day (e.g. before an IPO)."""


def has_history(symbol: str, day) -> bool:
    try:
        daily_context(symbol, day)
        return True
    except NoHistory:
        return False


@dataclass(frozen=True)
class DailyContext:
    symbol: str
    day: pd.Timestamp
    prev_close: float
    prev_high: float
    prev_low: float
    atr_d: float
    hist: pd.DataFrame          # daily bars before `day`, on `day`'s share basis

    def trendsell_setup(self) -> bool:
        """Spec 5-D / Trendsell [DOCUMENTED]: SMA10 < SMA20, SMA10 falling, and at
        least one day of higher highs (yesterday's high above the day before's).
        The trigger is a break below yesterday's low today."""
        c = self.hist["close"]
        s10, s20 = sma(c, 10), sma(c, 20)
        if len(c) < 22 or pd.isna(s20.iloc[-1]):
            return False
        h = self.hist["high"]
        return bool(s10.iloc[-1] < s20.iloc[-1] and s10.iloc[-1] < s10.iloc[-2] and h.iloc[-1] > h.iloc[-2])


@lru_cache(maxsize=4096)
def daily_context(symbol: str, day) -> DailyContext:
    day = pd.Timestamp(day).normalize()
    d = adjust_daily(daily_bars(symbol), symbol, day)
    hist = d[d.index < day].iloc[-300:]
    if len(hist) < 30:
        raise NoHistory(f"{symbol}: only {len(hist)} daily bars before {day:%Y-%m-%d}")
    last = hist.iloc[-1]
    return DailyContext(symbol, day, float(last["close"]), float(last["high"]), float(last["low"]),
                        float(atr(hist, 14).iloc[-1]), hist)
