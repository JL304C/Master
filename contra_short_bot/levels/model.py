"""The Level object and zone merging (spec 2, 3.4, 3.6)."""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

# When zones merge, the kept `kind` is the highest-ranked one present.
KIND_RANK = ["manual", "pivot", "swing", "trendline", "gap_fill", "gap_entry", "sma", "prior_high",
             "premarket_high", "psych"]


@dataclass
class Level:
    kind: str
    zone_low: float
    zone_high: float
    strength: float
    touches: int = 0
    first_seen: pd.Timestamp | None = None
    notes: str = ""
    labels: list[str] = field(default_factory=list)   # e.g. ["200SMA", "pivot"] for the prep sheet
    line: tuple | None = None                         # trendlines: (date0, price0, date1, price1)

    @property
    def mid(self) -> float:
        return (self.zone_low + self.zone_high) / 2

    def label(self) -> str:
        lo, hi = self.zone_low, self.zone_high
        price = f"{lo:.2f}-{hi:.2f}" if hi - lo >= 0.01 * self.mid else f"{self.mid:.2f}"
        return f"{price} ({', '.join(self.labels or [self.kind])})"


def _rank(kind: str) -> int:
    return KIND_RANK.index(kind) if kind in KIND_RANK else len(KIND_RANK)


def merge_levels(levels: list[Level], atr_d: float, merge_atr: float = 0.5,
                 sma_pivot_bonus: float = 2, psych_bonus: float = 1, max_width_atr: float = 1.0) -> list[Level]:
    """Merge zones closer than merge_atr x ATRd. The merged zone keeps the strongest
    kind and the summed strength; an SMA on a pivot gets +2, a round number on any
    other level gets +1. A merged zone may not grow wider than max_width_atr x ATRd,
    so a run of nearby levels doesn't chain into one huge band."""
    if not levels:
        return []
    gap = merge_atr * atr_d
    out: list[Level] = []
    group: list[Level] = []
    for lv in sorted(levels, key=lambda x: x.zone_low):
        too_far = group and lv.zone_low - max(g.zone_high for g in group) > gap
        too_wide = group and max(lv.zone_high, *(g.zone_high for g in group)) - group[0].zone_low > max_width_atr * atr_d
        if too_far or too_wide:
            out.append(_combine(group, sma_pivot_bonus, psych_bonus))
            group = []
        group.append(lv)
    out.append(_combine(group, sma_pivot_bonus, psych_bonus))
    return out


def _combine(group: list[Level], sma_pivot_bonus: float, psych_bonus: float) -> Level:
    if len(group) == 1:
        return group[0]
    kinds = {g.kind for g in group}
    best = min(group, key=lambda g: (_rank(g.kind), -g.strength))
    strength = sum(g.strength for g in group)
    if "sma" in kinds and kinds & {"pivot", "manual", "swing"}:
        strength += sma_pivot_bonus
    if "psych" in kinds:
        strength += psych_bonus
    labels = list(dict.fromkeys(l for g in sorted(group, key=lambda g: _rank(g.kind))
                                for l in (g.labels or [g.kind])))
    firsts = [g.first_seen for g in group if g.first_seen is not None]
    return Level(
        kind=best.kind,
        zone_low=min(g.zone_low for g in group),
        zone_high=max(g.zone_high for g in group),
        strength=strength,
        touches=sum(g.touches for g in group),
        first_seen=min(firsts) if firsts else None,
        notes="; ".join(g.notes for g in group if g.notes),
        labels=labels,
        line=best.line,
    )
