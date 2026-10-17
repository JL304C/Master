"""The Rejection Engine (spec 4), shared by strategies A, B, C and A0.

One `Setup` follows one resistance level on one stock through the states

    idle -> testing (or slow) -> pending entry -> in trade -> add pending -> managed
                 \\-> failed (closed beyond the zone too long)     \\-> second test

A0 is the same machine without a level: it starts when the stock goes into
FOCUS (a Contra Short print) and uses only the "stops going up" trigger.

Every decision is returned as an action with a reason string; nothing here
talks to a broker. The caller feeds completed 1-minute bars (`step`), fills
(`on_fill`) and position closes (`on_closed`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time

import numpy as np
import pandas as pd

from engine.types import Cancel, Fill, Log, MoveStop, Order, Place
from levels.model import Level
from risk.sizing import capped_add, full_size, planned_risk

TICK = 0.01


@dataclass
class Gate:
    """What the caller lets this setup do right now (watchlist, tone, time window)."""
    ok: bool
    size_mult: float = 1.0
    why: str = ""
    needs_climax: bool = False


class Focus:
    """State 1b: a Contra Short print puts the stock in FOCUS; every 1-minute
    higher high is watched until price stops going up."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.active = False
        self.start: pd.Timestamp | None = None
        self.vdif_start = self.peak_vdif = 0.0
        self.last_qs: pd.Timestamp | None = None
        self.qs_to_contra_min: float | None = None
        self.traded = False

    def update(self, t: pd.Timestamp, row) -> list[Log]:
        out = []
        if row.quickstrike_high:
            self.last_qs = t
        if row.contra_short > 0 and not self.active:
            self.active, self.start, self.traded = True, t, False
            self.vdif_start = self.peak_vdif = float(row.vdif)
            self.qs_to_contra_min = (t - self.last_qs).total_seconds() / 60 if self.last_qs is not None else None
            qs = f", Quickstrike {self.qs_to_contra_min:.0f} min earlier" if self.qs_to_contra_min is not None else ""
            out.append(f"FOCUS start: Contra Short tier {int(row.contra_short)}, Vdif {row.vdif:.2f}{qs}")
        elif self.active:
            self.peak_vdif = max(self.peak_vdif, float(row.vdif))
            timeout = (t - self.start) > pd.Timedelta(minutes=self.cfg["engine"]["focus_timeout_min"])
            fell_back = row.vdif < self.vdif_start - self.cfg["engine"]["focus_vdif_tolerance_atr5"] * row.atr5
            if not self.traded and (timeout or fell_back):
                self.active = False
                out.append(f"FOCUS end: {'30 min without a rejection' if timeout else 'Vdif back below its start'}"
                           f" (peak Vdif {self.peak_vdif:.2f})")
        return out


@dataclass
class Setup:
    symbol: str
    strategy: str                 # A | B | C | A0
    level: Level | None           # None for A0
    cfg: dict
    equity: float
    entry_mode: str = "scaled"
    state: str = "idle"
    attempts: int = 0
    was_below: bool = False
    first_spike: float | None = None
    spike: float | None = None
    outside: int = 0
    test_start: pd.Timestamp | None = None
    boosters: dict = field(default_factory=dict)
    order: Order | None = None
    order_age: int = 0
    fills: list = field(default_factory=list)       # (qty, price)
    orig_stop: float | None = None
    stop: float | None = None
    risk_budget: float = 0.0
    planned_full: int = 0
    entry_i: int = -1
    rej_i: int = -1
    add_tried: bool = False
    trigger: str = ""
    score: float = 0.0
    record: dict = field(default_factory=dict)      # facts about the trade, for the report

    # ---------------------------------------------------------------- helpers
    @property
    def zone(self) -> tuple[float, float]:
        if self.level is None:
            return (-np.inf, np.inf)
        return self.level.zone_low, self.level.zone_high

    def name(self) -> str:
        if self.level is None:
            return "Contra exhaustion (no level)"
        return f"{'/'.join(self.level.labels[:2])} {self.level.zone_low:.2f}-{self.level.zone_high:.2f}"

    def _log(self, text: str) -> Log:
        return Log(self.symbol, f"{self.strategy}: {text}")

    @property
    def shares(self) -> int:
        return sum(q for q, _ in self.fills)

    def busy(self) -> bool:
        """True while this setup has an order working or owns the position."""
        return self.state in ("pending", "in_trade", "add_pending", "managed", "confirm_wait")

    # ---------------------------------------------------------------- State 2
    def _extension(self, i: int, f: pd.DataFrame, row) -> str:
        e = self.cfg["engine"]["extension"]
        speed = self.cfg["strategies"].get(self.strategy, {}).get("speed_atr5", e["speed_atr5"])
        if row.rise_n >= speed * row.atr5:
            return f"speed {row.rise_n / row.atr5:.1f} x ATR5 in {e['speed_minutes']} min"
        t = f.index[i]
        if t.time() < (pd.Timestamp.combine(t.date(), time(9, 30)) + pd.Timedelta(minutes=e["open_minutes"])).time():
            return "spike into the zone at the open"
        lo = max(0, i - e["lookback_bars"] + 1)
        wide = (f["wide"].iloc[lo:i + 1] & ~f["red"].iloc[lo:i + 1]).astype(int)
        run = wide.groupby((wide == 0).cumsum()).cumsum().max()
        if run >= e["wide_body_bars"]:
            return f"{run} wide-bodied candles"
        return ""

    # ---------------------------------------------------------------- State 3
    def _boost(self, i: int, f: pd.DataFrame, row) -> None:
        b = self.boosters
        if row.hod_vol_1m or row.hod_vol_5m:
            b["volume_climax"] = 1
        if row.vol_ramp:
            b["volume_ramp"] = 1
        prev = f.iloc[i - 1] if i > 0 else None
        if prev is not None and prev.wide and row.body <= 0.5 * prev.body:
            b["body_constriction"] = 1
        if row.contra_short > 0:
            b["contra_tier"] = max(b.get("contra_tier", 0), int(row.contra_short))
        w = self.cfg["engine"]["booster_window_bars"]
        recent = f.iloc[max(0, i - w + 1):i + 1]
        if recent["quickstrike_high"].any() and (recent["contra_short"] > 0).any():
            b["quickstrike"] = 1
        if row.sp_top_reversal:
            b["sp_top_reversal"] = 1
        if row.gap_reversal:
            b["gap_reversal"] = 1

    def _score(self) -> float:
        w = self.cfg["strategies"]["boosters"]
        s = self.level.strength if self.level else 0.0
        return s + sum(w.get(k, 1) * v for k, v in self.boosters.items())

    # ---------------------------------------------------------------- State 4
    def _rejection(self, row, focus: Focus) -> str:
        zl, zh = self.zone
        a5 = row.atr5
        at_level = self.level is None or row.high >= zl - 0.5 * a5
        if not at_level or row.close >= zh:
            return ""
        if self.level is not None and (self.spike is None or self.spike < zl + self.cfg["engine"]["min_zone_reach"] * (zh - zl)):
            return ""   # price has not reached the level yet
        sgu = (focus.active and row.hh_run == 0 and row.hh_run_prev >= self.cfg["engine"]["stops_going_up_run"]
               and row.red)
        if sgu:
            return "stopped going up (first red bar without a higher high)"
        reach = zl + self.cfg["engine"]["min_zone_reach"] * (zh - zl)
        if self.level is not None and row.lower_half and self.spike is not None and self.spike >= reach:
            return "closed back inside the zone in the lower half of the bar"
        return ""

    # ---------------------------------------------------------------- main step
    def step(self, i: int, f: pd.DataFrame, row, focus: Focus, gate: Gate, can_enter: bool) -> list:
        t = f.index[i]
        out: list = []
        zl, zh = self.zone
        a5, a1 = row.atr5, row.atr1
        if self.state in ("done", "failed") or not (a5 > 0 and a1 > 0):
            return out
        E = self.cfg["engine"]

        if self.state == "pending" or self.state == "add_pending":
            self.order_age += 1
            if self.order_age >= self.order.valid_bars:
                out.append(Cancel(self.order.oid, f"{self.order.tag} not triggered in {self.order.valid_bars} bars"))
                out.append(self._log(f"{self.order.tag} sell-stop {self.order.price:.2f} expired"))
                self.order = None
                if self.state == "pending":
                    self._end_test(consumed=False)
                else:
                    self.state = "in_trade"
            return out

        if self.state in ("in_trade", "confirm_wait"):
            return out + self._confirm(i, f, row, gate)
        if self.state == "managed":
            return out

        # --- flat: idle / slow / testing
        # The approach must come from below: `was_below` is set by an earlier bar's
        # close under the zone, and cleared when price holds above the zone (the
        # level has flipped to support).
        approached = self.was_below
        if self.level is not None and self.state == "idle":
            self.outside = self.outside + 1 if row.close > zh else 0
            if self.outside > E["max_bars_outside"]:
                self.was_below = approached = False
            if row.close < zl:
                self.was_below = True
        if self.state == "idle":
            if self.attempts >= E["max_attempts"]:
                self.state = "done"
                return out
            if self.level is None:
                if not focus.active:
                    return out
                self.state, self.spike, self.test_start, self.boosters = "testing", row.high, t, {}
            elif approached and row.high >= zl:
                self.spike, self.outside, self.test_start, self.boosters = row.high, 0, t, {}
                ext = self._extension(i, f, row) if self.attempts == 0 else "second test"
                if not ext:
                    self.state = "slow"
                    out.append(self._log(f"slow approach into {self.name()}; first touch not traded"))
                else:
                    self.state = "testing"
                    self.record["extension"] = ext
                    out.append(self._log(f"test {self.attempts + 1} of {self.name()} ({ext})"))
            else:
                return out

        if self.state == "slow":
            if row.close < zl - E["leave_zone_atr5"] * a5:
                self.attempts += 1
                self.state = "idle"
            elif self._outside_failed(row, out):
                pass
            return out

        # testing
        self.spike = max(self.spike, row.high)
        if self._outside_failed(row, out):
            return out
        self._boost(i, f, row)
        if self.level is not None and row.close < zl - E["leave_zone_atr5"] * a5:
            out.append(self._log(f"test {self.attempts + 1} of {self.name()} ended without a rejection bar"))
            self._end_test(consumed=True)
            return out
        if self.level is None and not focus.active:
            self.state = "idle"
            return out
        trig = self._rejection(row, focus)
        if not trig:
            return out
        if self.attempts == 1 and self.first_spike is not None and self.spike >= self.first_spike:
            return out  # a second test must make a lower high
        if not can_enter:
            return out
        if not gate.ok or (gate.needs_climax and not self.boosters.get("volume_climax")):
            why = gate.why or "STRONG tone needs a volume climax"
            out.append(self._log(f"rejection at {self.name()} not traded: {why}"))
            self._end_test(consumed=True)
            return out
        self.trigger, self.rej_i = trig, i
        self.score = self._score()
        if self.attempts == 0:
            self.first_spike = self.spike
        self.attempts += 1
        b = E["scenario_b"]
        self.orig_stop = round(self.spike + max(b["atr1"] * a1, b["pct"] * row.close), 2)
        self.risk_budget = planned_risk(self.equity, self.cfg, gate.size_mult)
        self.record.update(test=self.attempts, trigger=trig, spike=self.spike, score=self.score,
                           boosters=dict(self.boosters), size_mult=gate.size_mult)
        which = "2nd" if self.attempts == 2 else "1st"
        if self.entry_mode == "confirmed_only":
            self.state = "confirm_wait"
            out.append(self._log(f"{which} test of {self.name()} rejected ({trig}); waiting for a 1m lower high"))
            return out
        entry = round(row.low - TICK, 2)
        self.planned_full = full_size(self.equity, entry, self.orig_stop, self.cfg, gate.size_mult)
        qty = self.planned_full if self.entry_mode == "aggressive_only" else self.planned_full // 3
        if qty <= 0:
            self.state = "idle"
            return out
        self.order = Order(self.symbol, "sell", "stop", qty, entry, E["entry_valid_bars"], self.strategy,
                           "entry1", f"{self.strategy}: {which} test of {self.name()} rejected ({trig}), "
                                     f"score {self.score:g} -> starter")
        self.order.placed, self.order_age, self.state = t, 0, "pending"
        out.append(Place(self.order))
        return out

    def _outside_failed(self, row, out: list) -> bool:
        if self.level is None:
            return False
        zl, zh = self.zone
        self.outside = self.outside + 1 if row.close > zh else 0
        if self.outside > self.cfg["engine"]["max_bars_outside"]:
            self.state = "failed"
            out.append(self._log(f"{self.name()} failed: {self.outside} closes above the zone; no more shorts here today"))
            return True
        return False

    def _end_test(self, consumed: bool) -> None:
        """A test ended without a position. `consumed`: it had not yet been counted
        as an attempt (a test without a rejection bar, or one the gate blocked).
        Attempts that issued an entry were counted at the rejection."""
        if consumed:
            if self.attempts == 0:
                self.first_spike = self.spike
            self.attempts += 1
        self.state = "idle" if self.attempts < self.cfg["engine"]["max_attempts"] else "done"
        self.spike, self.outside, self.boosters = None, 0, {}

    # ---------------------------------------------------------------- State 5
    def _lower_high(self, i: int, f: pd.DataFrame, after_i: int):
        """Bar i-1 is a 1-minute peak below the spike high, formed after `after_i`."""
        if i < 2 or i - 1 <= after_i:
            return None
        h = f["high"].to_numpy()
        if h[i - 1] > h[i - 2] and h[i - 1] >= h[i] and h[i - 1] < self.spike:
            return i - 1
        return None

    def _confirm(self, i: int, f: pd.DataFrame, row, gate: Gate) -> list:
        E = self.cfg["engine"]
        out: list = []
        t = f.index[i]
        if self.state == "confirm_wait" and i - self.rej_i > E["confirm_window_bars"]:
            out.append(self._log(f"no confirming lower high within {E['confirm_window_bars']} bars"))
            self._end_test(consumed=False)
            return out
        if self.add_tried and self.state == "in_trade":
            return out
        lh = self._lower_high(i, f, self.entry_i if self.state == "in_trade" else self.rej_i)
        if lh is None:
            return out
        if row.m2_higher_high:
            if self.state == "in_trade":
                self.add_tried = True
                out.append(self._log("2-minute chart still making higher highs (not aligned with 5-minute); "
                                     "add skipped, starter only"))
            return out
        if not row.m2_below_ema9:
            return out
        lh_bar = f.iloc[lh]
        stop_c = round(min(lh_bar.high + E["scenario_c_atr1"] * row.atr1, self.orig_stop), 2)
        entry = round(lh_bar.low - TICK, 2)
        if entry >= row.close:
            return out  # price is already below the trigger; the sell-stop would fill at market
        if self.state == "confirm_wait":
            qty = full_size(self.equity, entry, stop_c, self.cfg, gate.size_mult)
            self.planned_full = qty
            self.stop = stop_c
            tag, why = "entry", f"1m lower high {lh_bar.high:.2f} + 2m close below EMA9 -> full size"
            nxt = "pending"
        else:
            want = self.planned_full - self.shares
            qty = capped_add(self.risk_budget, self.fills, stop_c, entry, want)
            self.stop = stop_c
            tag, why = "entry2", f"1m lower high {lh_bar.high:.2f} + 2m close below EMA9 -> add"
            nxt = "add_pending"
            self.add_tried = True
        if qty <= 0:
            return out
        self.order = Order(self.symbol, "sell", "stop", qty, entry, E["add_valid_bars"], self.strategy, tag,
                           f"{self.strategy}: test {self.attempts} of {self.name()}: {why}")
        self.order.placed, self.order_age, self.state = t, 0, nxt
        out.append(Place(self.order))
        return out

    # ---------------------------------------------------------------- events
    def on_fill(self, fill: Fill, i: int) -> list:
        out: list = []
        tag = self.order.tag if self.order else ""
        self.fills.append((fill.qty, fill.price))
        self.order = None
        if tag == "entry1":
            self.entry_i, self.stop = i, self.orig_stop
            self.state = "managed" if self.entry_mode == "aggressive_only" else "in_trade"
            out.append(MoveStop(self.symbol, self.orig_stop, f"{self.strategy}: initial stop (Scenario B) above spike high {self.spike:.2f}"))
        elif tag == "entry":
            self.entry_i, self.state = i, "managed"
            out.append(MoveStop(self.symbol, self.stop, f"{self.strategy}: stop just inside the spike (Scenario C)"))
        elif tag == "entry2":
            self.state = "managed"
            out.append(MoveStop(self.symbol, self.stop, f"{self.strategy}: added; stop moved inside the spike (Scenario C)"))
        return out

    def on_closed(self, reason: str) -> list:
        """The position was closed (by the stop, a target, or the end of day)."""
        self.fills, self.order = [], None
        if reason == "stop" and self.attempts < self.cfg["engine"]["max_attempts"]:
            self.state, self.spike, self.outside, self.boosters, self.add_tried = "idle", None, 0, {}, False
            self.was_below = self.level is None
            return [self._log(f"stopped out; re-armed once for a second test of {self.name()}")]
        self.state = "done"
        return []
