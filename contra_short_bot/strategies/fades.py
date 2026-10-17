"""Strategies A, B and C (spec 5) are the Rejection Engine applied to the three
kinds of daily level. This module decides which strategy a level belongs to and
which levels are worth following on a given day.

  A  Daily pivot rejection fade: pivots, major swings, manual levels, and moving
     averages / 52-week high as lower-weight levels. Needs strength >= A.min_strength.
  B  Daily trendline rejection fade: falling resistance, broken rising support
     retested from below, channel upper rail. Extension relaxed to 1.5 x ATR5 and
     entry_mode confirmed_only by default.
  C  Gap-fill rejection fade: the top edge of an unfilled down-gap.
"""
from __future__ import annotations

from levels.library import DayLevels
from levels.model import Level


# Labels that make a level an A level. Prior-day highs/lows and round numbers only
# add strength (spec 3.4, 5-A): on their own they are not daily pivots.
A_LABELS = ("pivot", "swing high", "swing low", "double top", "double bottom", "manual", "52w high",
            "SMA", "EMA")


def strategy_for(level: Level) -> str:
    if level.kind == "gap_fill":
        return "C"
    if level.kind == "trendline":
        return "B"
    return "A"


def tradable_levels(dl: DayLevels, today_open: float, cfg: dict) -> list[tuple[str, Level]]:
    """Levels that can act as resistance today: within 3 x ATRd of the open on either
    side (a level below the open can be broken and retested from below, KITE style),
    passing each strategy's filter."""
    enabled = set(cfg["strategies"]["enabled"])
    reach = cfg["levels"]["max_open_above_atr"] * dl.atr_d
    out = []
    for lv in dl.levels:
        if abs(lv.mid - today_open) > reach:
            continue
        s = strategy_for(lv)
        if s not in enabled:
            continue
        if s == "A" and (lv.strength < cfg["strategies"]["A"]["min_strength"]
                         or not any(k in lab for lab in lv.labels for k in A_LABELS)):
            continue
        out.append((s, lv))
    return out
