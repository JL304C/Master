"""Simulated broker for the backtest (spec 10 fill model).

  * Sell-stop entries fill at the trigger, or at the bar's open if it gaps
    through, less 2 bps.
  * Buy-stop exits fill at the stop, or the open if it gaps through, plus 3 bps
    (+5 bps more if the stop sits within a tick of the obvious extreme).
  * Buy-limit targets fill only if price trades through the limit by $0.01.
  * When a stop and a target are both inside one bar, the stop fills first.
  * $0 commission and borrow (intraday, easy-to-borrow names only).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from engine.types import Order


@dataclass
class Pos:
    symbol: str
    strategy: str
    opened: pd.Timestamp
    fills: list = field(default_factory=list)     # (time, qty, price, tag)
    covers: list = field(default_factory=list)    # (time, qty, price, why)
    stop: float | None = None
    first_stop: float | None = None
    tm: object = None                             # TradeManager
    meta: dict = field(default_factory=dict)
    exit_next_open: str = ""
    open_i: int = 0

    @property
    def shares(self) -> int:
        return sum(q for _, q, _, _ in self.fills)

    @property
    def qty(self) -> int:
        return self.shares - sum(q for _, q, _, _ in self.covers)

    @property
    def avg(self) -> float:
        return sum(q * p for _, q, p, _ in self.fills) / max(1, self.shares)

    def pnl(self, mark: float | None = None) -> float:
        realized = sum(q * (self.avg - p) for _, q, p, _ in self.covers)
        return realized + (self.qty * (self.avg - mark) if mark is not None else 0.0)


class SimBroker:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.F = cfg["fills"]
        self.orders: dict[int, Order] = {}
        self.pos: dict[str, Pos] = {}

    # ---------------------------------------------------------------- prices
    def _entry_px(self, o: Order, bar) -> float:
        return round(min(o.price, bar.open) * (1 - self.F["entry_slip_bps"] / 1e4), 4)

    def _stop_px(self, p: Pos, bar) -> float:
        bps = self.F["stop_slip_bps"]
        spike = p.meta.get("spike")
        if spike is not None and p.stop - spike <= 0.01:
            bps += self.F["scenario_a_extra_bps"]
        return round(max(p.stop, bar.open) * (1 + bps / 1e4), 4)

    # ---------------------------------------------------------------- per bar
    def exits(self, sym: str, t: pd.Timestamp, bar) -> list[tuple[int, float, str]]:
        """Exits for an open position on this bar: (qty, price, why)."""
        p = self.pos.get(sym)
        if p is None:
            return []
        out = []
        if p.exit_next_open:
            return [(p.qty, bar.open * (1 + self.F["stop_slip_bps"] / 1e4), p.exit_next_open)]
        if p.stop is not None and bar.high >= p.stop:
            return [(p.qty, self._stop_px(p, bar), "stop")]
        if p.tm is not None:
            for k, tgt in p.tm.open_targets():
                if bar.low <= tgt.limit - self.F["limit_through"]:
                    q = min(p.qty, p.tm.qty_for(k, p.qty))
                    out.append((q, min(tgt.limit, bar.open), f"T{k + 1}"))
                    break   # at most one target per bar
        return out

    def entries(self, sym: str, bar) -> list[tuple[Order, float]]:
        out = []
        for oid, o in list(self.orders.items()):
            if o.symbol == sym and bar.low <= o.price:
                out.append((o, self._entry_px(o, bar)))
                del self.orders[oid]
        return out

    def busy_symbols(self) -> set[str]:
        return set(self.pos) | {o.symbol for o in self.orders.values()}
