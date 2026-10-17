"""Hard risk limits (spec 8). Strategies cannot override these: every new entry
order is checked here first."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RiskBook:
    cfg: dict
    equity: float
    day_realized: float = 0.0
    day_open: float = 0.0          # unrealized P&L of open positions at the last close
    halted: str = ""

    def allow_entry(self, symbol: str, has_position: bool, busy_symbols: set[str]) -> tuple[bool, str]:
        R = self.cfg["risk"]
        if R["kill_switch"]:
            return False, "kill switch on"
        if self.halted:
            return False, self.halted
        if self.day_realized + self.day_open <= -R["daily_loss_limit"] * self.equity:
            self.halted = f"daily loss limit ({-R['daily_loss_limit']:.1%}) hit: no new entries today"
            return False, self.halted
        if not has_position and len(busy_symbols - {symbol}) >= R["max_open_positions"]:
            return False, f"{R['max_open_positions']} positions/orders already open"
        return True, ""
