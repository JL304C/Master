"""Strategy D, downtrend continuation (spec 5-D; VERI example): after a sharp
drop, short pullbacks into the falling 5-minute EMA20, adding on later
pullbacks, and exit when the trend breaks."""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from engine.rejection import TICK, Gate
from engine.types import Cancel, ExitAll, Fill, Log, MoveStop, Order, Place
from risk.sizing import capped_add, full_size, planned_risk


@dataclass
class DownTrend:
    symbol: str
    cfg: dict
    equity: float
    prev_close: float
    state: str = "watching"          # watching | pending | in_trade | done
    move: str = ""                   # why the initial move qualified
    bars5: list = field(default_factory=list)   # completed 5m bars today: (time, o, h, l, c, ema20, atr5)
    order: Order | None = None
    order_age: int = 0
    fills: list = field(default_factory=list)
    entries: int = 0
    stop: float | None = None
    risk_budget: float = 0.0
    record: dict = field(default_factory=dict)

    strategy = "D"

    def _log(self, text: str) -> Log:
        return Log(self.symbol, f"D: {text}")

    def busy(self) -> bool:
        return self.state in ("pending", "in_trade")

    def _swing_highs(self) -> list[float]:
        """Confirmed 5-minute swing highs today (a bar above both neighbours)."""
        h = [b[2] for b in self.bars5]
        return [h[k] for k in range(1, len(h) - 1) if h[k] > h[k - 1] and h[k] >= h[k + 1]]

    def step(self, i: int, f: pd.DataFrame, row, gate: Gate, can_enter: bool) -> list:
        D = self.cfg["strategies"]["D"]
        out: list = []
        if self.state == "pending":
            self.order_age += 1
            if self.order_age >= self.order.valid_bars:
                out += [Cancel(self.order.oid, "pullback entry not triggered"),
                        self._log(f"sell-stop {self.order.price:.2f} expired")]
                self.order = None
                self.state = "in_trade" if self.fills else "watching"
        if not self.move:
            hi = max(float(f["high"].iloc[:i + 1].max()), self.prev_close)
            drop = hi - float(f["low"].iloc[:i + 1].min())
            if row.trendsell:
                self.move = "Trendsell: broke below yesterday's low"
            elif row.atr5 > 0 and drop >= D["drop_atr5"] * row.atr5 and row.rvol >= D["min_rvol"]:
                self.move = f"drop of {drop / row.atr5:.1f} x ATR5 on RVOL {row.rvol:.1f}"
            if self.move:
                out.append(self._log(f"initial move: {self.move}"))
        if not row.m5_done or pd.isna(row.m5_ema20):
            return out
        self.bars5.append((f.index[i], row.m5_open, row.m5_high, row.m5_low, row.m5_close, row.m5_ema20, row.atr5))
        ema_now = row.m5_ema20
        sh = self._swing_highs()

        # Exit: 5m close above the EMA20 and a break of the last lower high.
        if self.state == "in_trade" and sh and row.m5_close > ema_now and row.m5_high > sh[-1]:
            out.append(ExitAll(self.symbol, f"D: trend broken (5m close {row.m5_close:.2f} above EMA20 "
                                            f"{ema_now:.2f} and above the last lower high {sh[-1]:.2f})"))
            self.state = "done"
            return out

        if not self.move or self.state == "pending" or self.entries >= D["max_entries"] or not can_enter:
            return out
        lower = 0
        for k in range(len(sh) - 1, 0, -1):
            if sh[k] < sh[k - 1]:
                lower += 1
            else:
                break
        nb = D["ema_slope_bars"]
        falling = len(self.bars5) > nb and ema_now < self.bars5[-1 - nb][5]
        trend = lower >= D["lower_highs"] and row.m5_close < ema_now and falling
        pull = row.m5_high >= ema_now - D["ema_zone_atr5"] * row.atr5 and row.m5_close < ema_now
        if not (trend and pull):
            return out
        if not gate.ok:
            out.append(self._log(f"pullback to the 5m EMA20 not traded: {gate.why}"))
            return out
        entry = round(row.m5_low - TICK, 2)
        new_stop = round(row.m5_high + D["stop_atr5"] * row.atr5, 2)
        if entry >= row.close:
            return out
        if not self.fills:
            self.risk_budget = planned_risk(self.equity, self.cfg, gate.size_mult)
        stop = new_stop if self.stop is None else min(self.stop, new_stop)
        want = full_size(self.equity, entry, stop, self.cfg, gate.size_mult) // D["max_entries"]
        qty = capped_add(self.risk_budget, self.fills, stop, entry, want)
        if qty <= 0:
            return out
        n = self.entries + 1
        self.order = Order(self.symbol, "sell", "stop", qty, entry, D["entry_valid_bars"], "D",
                           "entry" if n == 1 else "add",
                           f"D: pullback {n} to the falling 5m EMA20 {ema_now:.2f} ({lower} lower highs; {self.move})")
        self.order.placed, self.order_age, self.state = f.index[i], 0, "pending"
        self._pending_stop = stop
        out.append(Place(self.order))
        return out

    def on_fill(self, fill: Fill, i: int) -> list:
        self.fills.append((fill.qty, fill.price))
        self.entries += 1
        self.order = None
        self.state = "in_trade"
        moved = self.stop is None or self._pending_stop < self.stop
        self.stop = self._pending_stop if self.stop is None else min(self.stop, self._pending_stop)
        return [MoveStop(self.symbol, self.stop, f"D: stop {'set' if self.entries == 1 else 'moved down'} "
                                                 f"above the pullback high")] if moved else []

    def on_closed(self, reason: str) -> list:
        self.fills, self.order, self.state = [], None, "done"
        return []
