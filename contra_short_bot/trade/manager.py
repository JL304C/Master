"""Trade Manager (spec 7): structure targets, exits in thirds, breakeven after
T1, and a trailing stop on the runner. One per open position.

Targets are the first support levels below the entry at least 1R, 2R and 3R
away: session VWAP, the low of day, today's 5-minute swing lows, daily support
zones and the prior day's low. Each limit sits a little above its support
("several ticks inside the level"). Strategy D has no fixed first target: it
only takes partial exits at structure levels.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from levels.library import DayLevels


@dataclass
class Target:
    label: str
    level: float        # the support price
    limit: float        # the buy-limit price, inside the level
    r: float            # distance from the entry in R
    filled: bool = False


@dataclass
class TradeManager:
    symbol: str
    strategy: str
    cfg: dict
    entry: float
    first_stop: float
    targets: list = field(default_factory=list)
    hits: int = 0
    stop: float | None = None

    @property
    def r(self) -> float:
        return max(self.first_stop - self.entry, 1e-9)

    def build(self, i: int, f: pd.DataFrame, levels: DayLevels) -> list[str]:
        """Pick T1-T3 when the position opens. Returns log lines."""
        T = self.cfg["trade"]
        row = f.iloc[i]
        a5 = float(row["atr5"]) if row["atr5"] == row["atr5"] else 0.0
        inside = max(T["inside_min"], T["inside_atr5"] * a5)
        cands: list[tuple[float, str]] = []
        if row["vwap"] < self.entry:
            cands.append((float(row["vwap"]), "VWAP"))
        lod = float(f["low"].iloc[:i + 1].min())
        if lod < self.entry:
            cands.append((lod, "low of day"))
        m5 = f.iloc[:i + 1]
        lows5 = m5["m5_low"].dropna().drop_duplicates()
        for k in range(1, len(lows5) - 1):
            if lows5.iloc[k] < lows5.iloc[k - 1] and lows5.iloc[k] <= lows5.iloc[k + 1]:
                cands.append((float(lows5.iloc[k]), "5m swing low"))
        for lv in levels.support(self.entry):
            cands.append((lv.zone_high, "/".join(lv.labels[:2])))
        cands = sorted({round(p, 2): lab for p, lab in cands if p < self.entry}.items(), key=lambda x: -x[0])

        ladder = [Target(f"{m:g}R", self.entry - m * self.r, round(self.entry - m * self.r, 2), m)
                  for m in T["ladder_r"]]
        out: list[Target] = []
        if T["target_mode"] == "risk_multiple":
            out = ladder
        else:
            # structure (spec default; "hybrid" is the same thing): the first support
            # at >= 1R, 2R, 3R ...
            used = set()
            for need in T["target_r"]:
                for p, lab in cands:
                    dist = (self.entry - p) / self.r
                    if dist >= need and p not in used:
                        out.append(Target(lab, p, round(p + inside, 2), round(dist, 2)))
                        used.add(p)
                        break
            if self.strategy != "D":
                # ... with the hybrid minimum: no support >= 1.5R below -> T1 = 2R.
                if not any((self.entry - p) / self.r >= T["hybrid_min_r"] for p, _ in cands):
                    out = []
                # Missing targets come from the 2R/3R/4R ladder, below the last one.
                for t in ladder:
                    if len(out) >= 3:
                        break
                    if not out or t.level < out[-1].level:
                        out.append(t)
        self.targets = sorted(out, key=lambda t: -t.level)[:3]
        return [f"targets: " + ", ".join(f"T{k + 1} {t.limit:.2f} ({t.label}, {t.r:.1f}R)"
                                          for k, t in enumerate(self.targets))] if self.targets else ["no targets"]

    def open_targets(self) -> list[tuple[int, Target]]:
        """T1 and T2 are working limit orders. The last third trails instead (spec
        7.1); T3 is logged as the next structure level for the report."""
        return [(k, t) for k, t in enumerate(self.targets[:2]) if not t.filled]

    def qty_for(self, k: int, position: int) -> int:
        """T1 covers 1/3 of the position, T2 half of what is left (another third)."""
        if position <= 1:
            return position
        return max(1, position // 3 if self.hits == 0 else position // 2)

    def on_target(self, k: int) -> float | None:
        """Returns the new stop after a target fill (breakeven after T1)."""
        self.targets[k].filled = True
        self.hits += 1
        if self.hits == self.cfg["trade"]["breakeven_after"]:
            return round(self.entry, 2)
        return None

    def trail(self, i: int, f: pd.DataFrame, since_i: int) -> float | None:
        """After T2: the runner's stop follows the latest 5-minute lower high."""
        if self.hits < 2:
            return None
        h = f["m5_high"].iloc[since_i:i + 1].dropna().drop_duplicates()
        swings = [h.iloc[k] for k in range(1, len(h) - 1) if h.iloc[k] > h.iloc[k - 1] and h.iloc[k] >= h.iloc[k + 1]]
        if not swings:
            return None
        a5 = float(f["atr5"].iloc[i])
        return round(float(swings[-1]) + self.cfg["trade"]["trail_atr5"] * a5, 2)

