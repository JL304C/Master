"""Per-stock orchestration: one SymbolTrader holds the stock's level setups
(A/B/C), its FOCUS tracker and A0 setup, and its Strategy D machine, and makes
sure only one of them has an order working or a position at a time.

`DayGate` applies the watchlist, the session window and the market-tone policy
(spec 6.1 table) to every entry decision. Backtest and live trading both use
these classes; only the feed and the order router differ.
"""
from __future__ import annotations

from datetime import time

import numpy as np
import pandas as pd

from data.fetch import load_config
from engine.rejection import Focus, Gate, Setup
from engine.types import Fill, Log
from levels.library import DayLevels
from selection.market_tone import Tone, live_tone, policy
from strategies.d_downtrend_continuation import DownTrend
from strategies.fades import tradable_levels


class DayGate:
    def __init__(self, day, premarket: Tone, level_strengths: list[float], cfg: dict | None = None):
        self.cfg = cfg or load_config()
        self.day = pd.Timestamp(day).normalize()
        self.premarket = premarket
        self.strengths = sorted(level_strengths)
        self._tone: dict = {}
        s = self.cfg["session"]
        self.first, self.last = time.fromisoformat(s["first_entry"]), time.fromisoformat(s["last_entry"])

    def tone(self, t: pd.Timestamp) -> Tone:
        """Live tone, refreshed every 5 minutes; the premarket tone until 09:35."""
        if t.time() < time(9, 35):
            return self.premarket
        k = t.floor(f"{self.cfg['selection']['tone']['live_every_min']}min")
        if k not in self._tone:
            self._tone[k] = live_tone(self.day, k.time(), self.cfg)
        return self._tone[k]

    def strength_cut(self, pct: float) -> float:
        return float(np.percentile(self.strengths, pct)) if self.strengths and pct > 0 else -np.inf

    def __call__(self, strategy: str, setup, t: pd.Timestamp, rel_weak_today: bool) -> Gate:
        if not (self.first <= t.time() < self.last):
            return Gate(False, why="outside the entry window")
        tone = self.tone(t)
        pol = policy(tone.label, self.cfg)
        if strategy == "D":
            if not pol["trend"]:
                return Gate(False, why=f"trend continuation off in a {tone.label} market")
            if pol["trend_needs_rel_weak"] and not rel_weak_today:
                return Gate(False, why=f"{tone.label} market: needs relative weakness vs SPY")
            return Gate(True, pol["size"])
        if not pol["fade"]:
            return Gate(False, why=f"fades off in a {tone.label} market")
        cut = self.strength_cut(pol["fade_min_strength_pct"])
        strength = setup.level.strength if setup.level is not None else -np.inf
        if strength < cut:
            return Gate(False, why=f"{tone.label} market: level strength {strength:g} below the top-"
                                   f"{100 - pol['fade_min_strength_pct']}% cut {cut:g}")
        return Gate(True, pol["size"], needs_climax=pol["fade_needs_climax"])


class SymbolTrader:
    def __init__(self, symbol: str, feat: pd.DataFrame, levels: DayLevels, gate: DayGate, equity: float,
                 on_watchlist: bool, cfg: dict | None = None):
        self.cfg = cfg or load_config()
        self.symbol, self.f, self.levels, self.gate = symbol, feat, levels, gate
        self.active = on_watchlist
        self.focus = Focus(self.cfg)
        modes = self.cfg["strategies"]["entry_mode"]
        enabled = set(self.cfg["strategies"]["enabled"])
        self.setups: list = [Setup(symbol, s, lv, self.cfg, equity, modes.get(s, "scaled"))
                             for s, lv in tradable_levels(levels, float(feat["open"].iloc[0]), self.cfg)]
        if "A0" in enabled:
            self.setups.append(Setup(symbol, "A0", None, self.cfg, equity, modes.get("A0", "scaled")))
        if "D" in enabled:
            self.setups.append(DownTrend(symbol, self.cfg, equity, levels.prior_close))
        self.owner = None
        self.rel_weak_today = False
        self.rows = list(feat.itertuples())

    def _join(self, i: int, row) -> list:
        """Spec 6.4 step 3: a stock not on the morning watchlist joins it intraday."""
        if self.active:
            return []
        why = ""
        if row.contra_short > 0:
            near = [lv for lv in self.levels.resistance(row.close)
                    if lv.zone_low - row.close <= self.cfg["selection"]["contra"]["level_atr5"] * row.atr5]
            if near or not self.cfg["selection"]["contra"]["requires_level"]:
                why = f"contra_t{int(row.contra_short)}" + (f" near {near[0].label()}" if near else "")
        elif row.rel_weak:
            why = "rel_weak"
        elif row.gap_down_go_down:
            why = "gap_down_go_down"
        if why:
            self.active = True
            return [Log(self.symbol, f"added to the watchlist intraday ({why})")]
        return []

    def step(self, i: int) -> list:
        row = self.rows[i]
        t = self.f.index[i]
        out: list = [Log(self.symbol, x) for x in self.focus.update(t, row)]
        out += self._join(i, row)
        self.rel_weak_today |= bool(row.rel_weak)
        for s in self.setups:
            can = self.active and (self.owner is None or self.owner is s)
            g = self.gate(s.strategy, s, t, self.rel_weak_today) if can else Gate(False)
            if isinstance(s, DownTrend):
                out += s.step(i, self.f, row, g, can)
            else:
                out += s.step(i, self.f, row, self.focus, g, can)
            if s.busy():
                self.owner = s
            elif self.owner is s:
                self.owner = None
        return out

    def on_fill(self, fill: Fill, i: int) -> list:
        if self.owner is None:
            return []
        if getattr(self.owner, "level", 1) is None or self.focus.active:
            self.focus.traded = True
        return self.owner.on_fill(fill, i)

    def on_closed(self, reason: str) -> list:
        if self.owner is None:
            return []
        out = self.owner.on_closed(reason)
        if not self.owner.busy():
            self.owner = None
        return out
