"""Opening box and break/snapback setup detection on 1-hour bars."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .data import resample


@dataclass
class Market:
    """5-minute and 1-hour bars as numpy arrays, with the 5-min slice of each hour."""
    t5: np.ndarray
    o5: np.ndarray
    h5: np.ndarray
    l5: np.ndarray
    c5: np.ndarray
    t1: pd.DatetimeIndex
    o1: np.ndarray
    h1: np.ndarray
    l1: np.ndarray
    c1: np.ndarray
    s5: np.ndarray   # first 5-min index of each hour
    e5: np.ndarray   # one past the last 5-min index of each hour
    inst1: np.ndarray | None  # instrument id per hour (to detect contract rolls)


def build_market(bars5: pd.DataFrame, hour_offset_min: int = 0) -> Market:
    """bars5: 5-minute OHLC bars (or 1-hour bars if that's all there is) indexed by ET time."""
    offset = f"{hour_offset_min}min" if hour_offset_min else None
    hourly = resample(bars5, "60min", offset)
    t5 = bars5.index.as_unit("ns").asi8
    t1 = hourly.index.as_unit("ns")
    s5 = np.searchsorted(t5, t1.asi8, side="left")
    e5 = np.searchsorted(t5, (t1 + pd.Timedelta(minutes=60)).asi8, side="left")
    inst = hourly["instrument_id"].to_numpy() if "instrument_id" in hourly else None
    return Market(
        t5, bars5["open"].to_numpy(float), bars5["high"].to_numpy(float),
        bars5["low"].to_numpy(float), bars5["close"].to_numpy(float),
        t1, hourly["open"].to_numpy(float), hourly["high"].to_numpy(float),
        hourly["low"].to_numpy(float), hourly["close"].to_numpy(float), s5, e5, inst,
    )


def box_indices(m: Market, box_open_time: str, box_tz: str) -> np.ndarray:
    """Indices of hourly bars that start at box_open_time in box_tz (DST-aware)."""
    hh, mm = (int(x) for x in box_open_time.split(":"))
    local = m.t1.tz_convert(box_tz)
    return np.flatnonzero((local.hour == hh) & (local.minute == mm))


@dataclass
class Session:
    box_i: int          # hourly index of the box candle
    end_i: int          # hourly index where the trading day ends (next box candle), exclusive
    date: pd.Timestamp  # trading date label
    box_high: float
    box_low: float
    status: str = "ok"  # ok | zero_height | roll | skipped_size


@dataclass
class Setup:
    session: Session
    direction: int      # +1 long, -1 short
    break_i: int
    entry_i: int        # hourly bar whose close is the entry
    entry_raw: float    # that bar's close, before slippage
    snap_low: float     # entry candle's low (long) / high (short) -> fixed targets
    snap_high: float


def sessions(m: Market, p: dict) -> list[Session]:
    idx = box_indices(m, p["box_open_time"], p["box_tz"])
    out = []
    for k, b in enumerate(idx):
        end = idx[k + 1] if k + 1 < len(idx) else len(m.t1)
        date = (m.t1[b] + pd.Timedelta(hours=8)).normalize().tz_localize(None)
        s = Session(int(b), int(end), date, m.h1[b], m.l1[b])
        if s.box_high - s.box_low <= 0:
            s.status = "zero_height"
        elif m.inst1 is not None and len(set(m.inst1[b:end])) > 1:
            s.status = "roll"
        out.append(s)

    pct = p.get("skip_pct")
    if pct:
        look = p.get("skip_lookback", 50)
        hs = np.array([s.box_high - s.box_low for s in out])
        for k in range(look, len(out)):
            prev = hs[k - look:k]
            lo, hi = np.percentile(prev, pct), np.percentile(prev, 100 - pct)
            if out[k].status == "ok" and not (lo <= hs[k] <= hi):
                out[k].status = "skipped_size"
        for s in out[:look]:
            if s.status == "ok":
                s.status = "skipped_size"  # not enough history to judge box size
    return out


def find_setup(m: Market, s: Session, p: dict) -> tuple[Setup | None, str]:
    """Walk the session's hourly bars and return the first valid entry, or why there was none."""
    bh, bl = s.box_high, s.box_low
    H = bh - bl
    td = p["touch_depth"]
    wait = p["max_wait_bars"]
    turn = p["require_turn_candle"]
    reentry = p["allow_reentry_after_close_inside"]
    breakout = p["entry_mode"] == "breakout"

    # per direction: state in {idle, armed, wait_inside, dead}
    state = {1: "idle", -1: "idle"}
    brk = {1: -1, -1: -1}
    saw_break = False

    def close_inside_or_dead(d):
        state[d] = "idle" if reentry else "dead"

    for j in range(s.box_i + 1, s.end_i):
        o, h, l, c = m.o1[j], m.h1[j], m.l1[j], m.c1[j]
        side = 1 if c > bh else (-1 if c < bl else 0)

        for d in (1, -1):
            if state[d] == "armed":
                if side == -d:                      # closed beyond the opposite side
                    state[d] = "dead"
                elif side == 0:                     # closed back inside before a snapback
                    close_inside_or_dead(d)
                else:
                    touched = (l <= bh - td * H) if d == 1 else (h >= bl + td * H)
                    turned = (c > o) if d == 1 else (c < o)
                    if touched and (turned or not turn):
                        return Setup(s, d, brk[d], j, c, l, h), "entry"
                    if j - brk[d] >= wait:
                        state[d] = "wait_inside" if reentry else "dead"
            elif state[d] == "wait_inside" and side == 0:
                state[d] = "idle"
            elif state[d] == "idle" and side == d:
                saw_break = True
                if breakout:
                    return Setup(s, d, j, j, c, l, h), "entry"
                state[d] = "armed"
                brk[d] = j
    return None, ("no_snapback" if saw_break else "no_break")
