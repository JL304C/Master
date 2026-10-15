"""Trade simulation: stops, breakeven, trailing, targets, costs and sizing.

Shorts are simulated as longs on negated prices, so every rule is written once.
Stops and targets are checked on 5-minute bars; breakeven and trailing stops
move only at each closed 1-hour candle. If a 5-minute bar touches both the stop
and a target, the stop is assumed to have been hit first.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .box import Market, Session, Setup, find_setup, sessions


@dataclass
class Instrument:
    name: str
    point_value: float
    tick_size: float
    commission_per_side: float
    slippage_ticks: float

    @property
    def slip(self) -> float:
        return self.tick_size * self.slippage_ticks


def plan_levels(st: Setup, p: dict) -> tuple[float, list[tuple[float, float]]]:
    """Initial stop and [(target_price, fraction)] in raw prices for a setup."""
    s, d = st.session, st.direction
    H = s.box_high - s.box_low
    if d == 1:
        stop = s.box_high - p["stop_k"] * H
        base = st.snap_low
    else:
        stop = s.box_low + p["stop_k"] * H
        base = st.snap_high
    entry = st.entry_raw
    mode = p["target_mode"]
    if mode == "fixed":
        tp1, tp2 = base + d * H, base + d * 2 * H
        targets = [(tp1, 0.5), (tp2, 0.5)] if p["partials"] else [((tp1 if p["fixed_tp"] == 1 else tp2), 1.0)]
    elif mode == "rr":
        targets = [(entry + p["rr_target"] * (entry - stop), 1.0)]
    elif mode == "hold":
        targets = []
    else:
        raise ValueError(f"unknown target_mode {mode}")
    return stop, targets


def simulate_trade(m: Market, st: Setup, p: dict, inst: Instrument,
                   stop_raw: float, targets_raw: list[tuple[float, float]], direction: int,
                   last_hour: int) -> dict:
    """Simulate one trade from the close of st.entry_i through hour `last_hour` (inclusive)."""
    d = direction
    H = st.session.box_high - st.session.box_low
    slip = inst.slip
    # negated price space for shorts
    o5, h5, l5, c5 = (m.o5, m.h5, m.l5, m.c5) if d == 1 else (-m.o5, -m.l5, -m.h5, -m.c5)
    h1, l1, c1 = (m.h1, m.l1, m.c1) if d == 1 else (-m.l1, -m.h1, -m.c1)

    entry = d * st.entry_raw + slip
    stop = d * stop_raw
    init_stop = stop
    targets = sorted([(d * px, f) for px, f in targets_raw])
    risk_pts = entry - stop
    if risk_pts <= 0:
        return {"skip": "stop_beyond_entry"}

    be_trig = p["be_trigger"]
    be_done = be_trig is None
    trail = p["trail_mode"]
    anchor_px = entry - slip + (be_trig or 0) * H
    anchor_stop = None if be_trig is not None else init_stop
    best_high = entry - slip
    max_close = entry - slip
    prev_hi, prev_hi_j = h1[st.entry_i], st.entry_i
    be_time = None

    remaining = 1.0
    fills = []  # (fraction, price, t5 index, reason)

    for j in range(st.entry_i + 1, last_hour + 1):
        for i in range(m.s5[j], m.e5[j]):
            if o5[i] <= stop:
                fills.append((remaining, o5[i] - slip, i, "stop_gap"))
                remaining = 0.0
            elif l5[i] <= stop:
                fills.append((remaining, stop - slip, i, "stop"))
                remaining = 0.0
            else:
                while targets and remaining > 1e-9:
                    px, f = targets[0]
                    if h5[i] < px:
                        break
                    f = min(f, remaining)
                    fill = max(px, o5[i]) - slip
                    fills.append((f, fill, i, "target"))
                    remaining -= f
                    targets.pop(0)
            if remaining <= 1e-9:
                break
        if remaining <= 1e-9:
            break

        # hourly close: breakeven then trailing (stop only moves in the trade's favour)
        hh, hl, hc = h1[j], l1[j], c1[j]
        max_close = max(max_close, hc)
        if not be_done and hc >= entry - slip + be_trig * H:
            be_done = True
            be_time = j
            stop = max(stop, entry - slip + p["be_buffer"] * H)
            anchor_stop = entry - slip + p["be_buffer"] * H
        if be_done:
            if trail == "swing":
                if hh > prev_hi and j - prev_hi_j > 1:
                    pull_low = l1[prev_hi_j + 1:j].min()
                    stop = max(stop, pull_low - p["trail_buffer"] * H)
            elif trail == "step":
                n = math.floor((max_close - anchor_px) / (p["trail_step"] * H))
                if n >= 1:
                    stop = max(stop, anchor_stop + n * p["trail_step"] * H)
            elif trail == "candle":
                stop = max(stop, hl)
            elif trail == "points":
                stop = max(stop, max(best_high, hh) - p["trail_points"] * H)
            elif trail != "off":
                raise ValueError(f"unknown trail_mode {trail}")
        best_high = max(best_high, hh)
        if hh > prev_hi:
            prev_hi, prev_hi_j = hh, j

    if remaining > 1e-9:
        i = m.e5[last_hour] - 1
        fills.append((remaining, c5[i] - slip, i, "session_end"))

    pts = sum(f * (px - entry) for f, px, _, _ in fills)
    pv, comm = inst.point_value, inst.commission_per_side
    r_net = (pts * pv - 2 * comm) / (risk_pts * pv)
    last = fills[-1]
    reasons = "+".join(dict.fromkeys(r for _, _, _, r in fills))
    return {
        "direction": "long" if d == 1 else "short",
        "entry_price": d * entry,
        "stop": d * init_stop,
        "targets": ";".join(f"{px:.2f}" for px, _ in targets_raw),
        "exit_time": pd.Timestamp(m.t5[last[2]], tz="UTC").tz_convert(m.t1.tz) + pd.Timedelta(minutes=5),
        "exit_price": d * (sum(f * px for f, px, _, _ in fills)),
        "exit_reason": reasons,
        "be_hit": be_time is not None,
        "first_target_R": ((d * targets_raw[0][0] - entry) / risk_pts) if targets_raw else float("nan"),
        "risk_pts": risk_pts,
        "pts": pts,
        "R": r_net,
        "R_gross": pts / risk_pts,
        "exit_i5": last[2],
    }


def detect(m: Market, p: dict) -> tuple[list[Session], list[tuple[Session, Setup | None, str]]]:
    ss = sessions(m, p)
    out = []
    for s in ss:
        if s.status != "ok":
            out.append((s, None, s.status))
            continue
        st, why = find_setup(m, s, p)
        out.append((s, st, why))
    return ss, out


def run(m: Market, p: dict, inst: Instrument, acct: dict, detected=None,
        flip_rng: np.random.Generator | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Run the strategy. Returns (trades, days). `flip_rng` gives the random-direction baseline."""
    if detected is None:
        _, detected = detect(m, p)
    equity = acct["starting_equity"]
    risk_frac = acct["risk_per_trade"]
    busy_until = -1
    trades, days = [], []
    for s, st, why in detected:
        H = s.box_high - s.box_low
        day = {"date": s.date, "box_high": s.box_high, "box_low": s.box_low, "H": H, "outcome": why}
        if st is None:
            days.append(day)
            continue
        if m.s5[st.entry_i] <= busy_until:
            day["outcome"] = "position_open"
            days.append(day)
            continue
        last_hour = s.end_i - 1 if p["eod_exit"] else len(m.t1) - 1
        if st.entry_i >= last_hour:
            day["outcome"] = "entry_at_session_end"
            days.append(day)
            continue
        stop, targets = plan_levels(st, p)
        d = st.direction
        if flip_rng is not None and flip_rng.random() < 0.5:
            d = -d
            stop = 2 * st.entry_raw - stop
            targets = [(2 * st.entry_raw - px, f) for px, f in targets]
        res = simulate_trade(m, st, p, inst, stop, targets, d, last_hour)
        if "skip" in res:
            day["outcome"] = res["skip"]
            days.append(day)
            continue
        per_contract_risk = res["risk_pts"] * inst.point_value
        qty = equity * risk_frac / per_contract_risk
        if not acct.get("fractional_size", True):
            qty = math.floor(qty)
            if qty < 1:
                day["outcome"] = "size_below_one_contract"
                days.append(day)
                continue
        pnl = qty * (res["pts"] * inst.point_value - 2 * inst.commission_per_side)
        equity += pnl
        busy_until = res.pop("exit_i5")
        trades.append({
            "date": s.date, "instrument": inst.name, "direction": res["direction"],
            "box_high": s.box_high, "box_low": s.box_low, "H": H,
            "break_time": m.t1[st.break_i] + pd.Timedelta(hours=1),
            "entry_time": m.t1[st.entry_i] + pd.Timedelta(hours=1),
            "entry_price": res["entry_price"], "stop": res["stop"], "targets": res["targets"],
            "exit_time": res["exit_time"], "exit_price": res["exit_price"], "exit_reason": res["exit_reason"],
            "be_hit": res["be_hit"], "first_target_R": res["first_target_R"], "R": res["R"], "R_gross": res["R_gross"],
            "qty": qty, "pnl": pnl, "equity": equity,
        })
        day["outcome"] = "trade"
        days.append(day)
    return pd.DataFrame(trades), pd.DataFrame(days)
