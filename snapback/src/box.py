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
    hr5: np.ndarray = None    # hour index of each 5-min bar


def build_market(bars5: pd.DataFrame, hour_offset_min: int = 0) -> Market:
    """bars5: 5-minute OHLC bars (or 1-hour bars if that's all there is) indexed by ET time."""
    offset = f"{hour_offset_min}min" if hour_offset_min else None
    hourly = resample(bars5, "60min", offset)
    t5 = bars5.index.as_unit("ns").asi8
    t1 = hourly.index.as_unit("ns")
    s5 = np.searchsorted(t5, t1.asi8, side="left")
    e5 = np.searchsorted(t5, (t1 + pd.Timedelta(minutes=60)).asi8, side="left")
    inst = hourly["instrument_id"].to_numpy() if "instrument_id" in hourly else None
    hr5 = np.searchsorted(t1.asi8, t5, side="right") - 1
    return Market(
        t5, bars5["open"].to_numpy(float), bars5["high"].to_numpy(float),
        bars5["low"].to_numpy(float), bars5["close"].to_numpy(float),
        t1, hourly["open"].to_numpy(float), hourly["high"].to_numpy(float),
        hourly["low"].to_numpy(float), hourly["close"].to_numpy(float), s5, e5, inst, hr5,
    )


def ts5(m: Market, i: int) -> pd.Timestamp:
    return pd.Timestamp(m.t5[i], tz="UTC").tz_convert(m.t1.tz)


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
    entry_i: int        # hourly bar containing the entry
    entry_raw: float    # entry bar's close, before slippage
    snap_low: float     # entry candle's low (long) / high (short) -> fixed targets
    snap_high: float
    entry_i5: int = -1  # 5-min bar whose close is the entry (last 5-min bar of entry_i for Variant A)
    break_time: pd.Timestamp | None = None
    entry_time: pd.Timestamp | None = None


def sessions(m: Market, p: dict) -> list[Session]:
    idx = box_indices(m, p["box_open_time"], p["box_tz"])
    out = []
    for k, b in enumerate(idx):
        end = idx[k + 1] if k + 1 < len(idx) else len(m.t1)
        date = (m.t1[b] + pd.Timedelta(hours=8)).normalize().tz_localize(None)
        s = Session(int(b), int(end), date, m.h1[b], m.l1[b])
        if s.box_high - s.box_low <= 0:
            s.status = "zero_height"
        elif m.inst1 is not None and len(set(m.inst1[b:_trading_end_i(m, s, p)])) > 1:
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


def _trading_end_i(m: Market, s: Session, p: dict) -> int:
    """Hourly index where trading stops for the day (rolls after this don't matter unless holding overnight)."""
    if not p.get("eod_exit", True):
        return s.end_i
    return int(np.searchsorted(m.t1.asi8, day_end_time(m, s, p).as_unit("ns").value, side="left"))


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
                        return _hourly_setup(m, s, d, brk[d], j), "entry"
                    if j - brk[d] >= wait:
                        state[d] = "wait_inside" if reentry else "dead"
            elif state[d] == "wait_inside" and side == 0:
                state[d] = "idle"
            elif state[d] == "idle" and side == d:
                saw_break = True
                if breakout:
                    return _hourly_setup(m, s, d, j, j), "entry"
                state[d] = "armed"
                brk[d] = j
    return None, ("no_snapback" if saw_break else "no_break")


def _hourly_setup(m: Market, s: Session, d: int, brk: int, j: int) -> Setup:
    hour = pd.Timedelta(hours=1)
    return Setup(s, d, brk, j, m.c1[j], m.l1[j], m.h1[j], int(m.e5[j] - 1), m.t1[brk] + hour, m.t1[j] + hour)


def day_end_time(m: Market, s: Session, p: dict) -> pd.Timestamp:
    """When the trading day ends: eod_time (e.g. 16:00 ET) on the box's day, else the next box candle."""
    nxt = m.t1[s.end_i] if s.end_i < len(m.t1) else m.t1[-1] + pd.Timedelta(hours=1)
    if p.get("eod_time"):
        hh, mm = (int(x) for x in p["eod_time"].split(":"))
        return min(m.t1[s.box_i].normalize() + pd.Timedelta(hours=hh, minutes=mm), nxt)
    return nxt


def find_setup_b(m: Market, s: Session, p: dict) -> tuple[Setup | None, str]:
    """Variant B: 5-minute close beyond the box, then a 5-minute pullback that touches the box edge,
    closes back outside, and has a long wick on the box side. Entry at that bar's close."""
    bh, bl = s.box_high, s.box_low
    H = bh - bl
    td = p["touch_depth"]
    wr = p.get("min_wick_ratio")
    breakout = p["entry_mode"] == "breakout"
    cancel_inside = p.get("cancel_on_close_inside", False)
    reentry = p["allow_reentry_after_close_inside"]

    box_close = m.t1[s.box_i] + pd.Timedelta(hours=1)
    end = day_end_time(m, s, p)
    if p.get("b_window_hours"):
        end = min(end, box_close + pd.Timedelta(hours=p["b_window_hours"]))
    i0 = int(m.e5[s.box_i])
    i1 = int(np.searchsorted(m.t5, end.as_unit("ns").value, side="left"))

    state = {1: "idle", -1: "idle"}
    brk = {1: -1, -1: -1}
    saw_break = False
    for i in range(i0, i1):
        o, h, l, c = m.o5[i], m.h5[i], m.l5[i], m.c5[i]
        side = 1 if c > bh else (-1 if c < bl else 0)
        for d in (1, -1):
            if state[d] == "armed":
                if side == -d:
                    state[d] = "dead"
                elif side == 0:
                    if cancel_inside:
                        state[d] = "idle" if reentry else "dead"
                else:
                    touched = (l <= bh - td * H) if d == 1 else (h >= bl + td * H)
                    rng = h - l
                    wick = (min(o, c) - l) if d == 1 else (h - max(o, c))
                    if touched and (wr is None or (rng > 0 and wick / rng >= wr)):
                        return _five_min_setup(m, s, d, brk[d], i), "entry"
            elif state[d] == "idle" and side == d:
                saw_break = True
                if breakout:
                    return _five_min_setup(m, s, d, i, i), "entry"
                state[d] = "armed"
                brk[d] = i
    return None, ("no_pullback" if saw_break else "no_break")


def _five_min_setup(m: Market, s: Session, d: int, brk: int, i: int) -> Setup:
    five = pd.Timedelta(minutes=5)
    return Setup(s, d, int(m.hr5[brk]), int(m.hr5[i]), m.c5[i], m.l5[i], m.h5[i], i,
                 ts5(m, brk) + five, ts5(m, i) + five)
