"""Build the day's level list for one symbol (spec 3). Uses only daily bars that
closed before `day`, on the share basis in force on `day`."""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from data.bars import adjust_daily, daily_bars
from data.fetch import ROOT, load_config
from indicators.core import atr
from levels.model import Level, merge_levels
from levels.pivots import find_pivots
from levels.structure import find_gaps, find_mas, find_prior, find_psych, manual_levels
from levels.trendlines import find_trendlines


@dataclass
class DayLevels:
    symbol: str
    day: pd.Timestamp
    prior_close: float
    atr_d: float
    levels: list[Level]          # merged, sorted low -> high
    raw: list[Level]             # before merging, for charts and debugging

    def resistance(self, price: float | None = None) -> list[Level]:
        """Levels whose zone top is above `price` (default: prior close), nearest first."""
        p = self.prior_close if price is None else price
        return sorted([lv for lv in self.levels if lv.zone_high > p], key=lambda lv: lv.zone_low)

    def support(self, price: float | None = None) -> list[Level]:
        p = self.prior_close if price is None else price
        return sorted([lv for lv in self.levels if lv.zone_high <= p], key=lambda lv: -lv.zone_high)

    def tradable_resistance(self, today_open: float) -> list[Level]:
        """Spec 3.6: skip levels the stock opened more than 3 x ATRd above."""
        lim = load_config()["levels"]["max_open_above_atr"] * self.atr_d
        return [lv for lv in self.resistance(min(today_open, self.prior_close)) if today_open - lv.zone_high <= lim]


def build_levels_from_daily(daily: pd.DataFrame, symbol: str, day: pd.Timestamp, cfg: dict | None = None,
                            manual_path=None) -> DayLevels:
    cfg = cfg or load_config()
    L = cfg["levels"]
    hist = daily[daily.index < day].iloc[-L["lookback_days"]:]
    if len(hist) < 60:
        raise ValueError(f"{symbol} {day:%Y-%m-%d}: only {len(hist)} daily bars before this day")
    a = atr(hist, L["atr_period"])
    atr_d = float(a.iloc[-1])
    raw: list[Level] = []
    raw += find_pivots(hist, a, cfg)
    raw += find_trendlines(hist, a, cfg)
    raw += find_gaps(hist, a, cfg)
    raw += find_mas(hist, a, cfg)
    raw += find_psych(hist, a, cfg)
    raw += find_prior(hist, a, cfg)
    raw += manual_levels(manual_path or ROOT / "manual_levels.yaml", symbol, day, atr_d, cfg)
    merged = merge_levels(raw, atr_d, L["merge_atr"], L["ma"]["pivot_overlap_bonus"],
                           L["psych"]["confluence_bonus"], L["merge_max_width_atr"])
    return DayLevels(symbol, day, float(hist["close"].iloc[-1]), atr_d, merged, raw)


def build_levels(symbol: str, day, cfg: dict | None = None) -> DayLevels:
    """Levels for `symbol` before the open on `day`."""
    day = pd.Timestamp(day).normalize()
    daily = adjust_daily(daily_bars(symbol), symbol, day)
    return build_levels_from_daily(daily, symbol, day, cfg)
