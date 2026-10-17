"""Position sizing (spec 8): shares = floor(planned risk / (stop - entry)), capped by notional."""
from __future__ import annotations

import math


def planned_risk(equity: float, cfg: dict, size_mult: float = 1.0) -> float:
    return equity * cfg["risk"]["risk_per_trade"] * size_mult


def full_size(equity: float, entry: float, stop: float, cfg: dict, size_mult: float = 1.0) -> int:
    per_share = stop - entry
    if per_share <= 0 or entry <= 0:
        return 0
    by_risk = planned_risk(equity, cfg, size_mult) / per_share
    by_notional = equity * cfg["risk"]["max_notional"] / entry
    return max(0, math.floor(min(by_risk, by_notional)))


def capped_add(risk_budget: float, fills: list[tuple[int, float]], new_stop: float, add_entry: float, want: int) -> int:
    """Largest add (<= want) that keeps total risk of all shares to `new_stop`
    within `risk_budget` (spec 4, State 5 risk cap)."""
    used = sum(q * max(0.0, new_stop - px) for q, px in fills)
    per = new_stop - add_entry
    if per <= 0:
        return want
    room = risk_budget - used
    return max(0, min(want, math.floor(room / per)))
