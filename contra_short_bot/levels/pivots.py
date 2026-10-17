"""Daily pivot zones (spec 3.1): clusters of swing highs, swing lows and
consolidation closes that line up at the same price."""
from __future__ import annotations

import numpy as np
import pandas as pd

from levels.model import Level


def swing_points(daily: pd.DataFrame, k: int) -> pd.DataFrame:
    """Swing highs/lows: a bar whose high (low) is beyond the k bars on each side.
    The last k bars can't be confirmed yet, so they never qualify (no lookahead).
    A tie with a later bar still counts, so flat double-bar tops are found."""
    h, l = daily["high"].to_numpy(), daily["low"].to_numpy()
    rows = []
    for i in range(k, len(daily) - k):
        if h[i] > h[i - k:i].max() and h[i] >= h[i + 1:i + k + 1].max():
            rows.append((daily.index[i], h[i], "swing_high"))
        if l[i] < l[i - k:i].min() and l[i] <= l[i + 1:i + k + 1].min():
            rows.append((daily.index[i], l[i], "swing_low"))
    return pd.DataFrame(rows, columns=["date", "price", "src"])


def consolidation_closes(daily: pd.DataFrame, atr_d: pd.Series, days: int, tight_atr: float) -> pd.DataFrame:
    """The close at the end of each run of >= `days` sessions whose closes stay
    within tight_atr x ATRd of each other."""
    c = daily["close"].to_numpy()
    a = atr_d.to_numpy()
    rows, start = [], 0
    for i in range(1, len(c) + 1):
        if i < len(c) and not np.isnan(a[i]) and c[start:i + 1].max() - c[start:i + 1].min() <= tight_atr * a[i]:
            continue
        if i - start >= days:
            rows.append((daily.index[i - 1], c[i - 1], "consolidation"))
        start = i
    return pd.DataFrame(rows, columns=["date", "price", "src"])


def find_pivots(daily: pd.DataFrame, atr_d: pd.Series, cfg: dict) -> list[Level]:
    """`daily` holds only bars before the trading day (adjusted to its share basis)."""
    p = cfg["levels"]["pivot"]
    a = float(atr_d.iloc[-1])
    pts = pd.concat([swing_points(daily, p["swing_bars"]),
                     consolidation_closes(daily, atr_d, p["consolidation_days"], p["consolidation_atr"])],
                    ignore_index=True)
    if pts.empty:
        return []
    vol_avg = daily["volume"].rolling(50, min_periods=20).mean()
    hv = (daily["volume"] >= p["high_volume_mult"] * vol_avg)
    pts["high_vol"] = pts["date"].map(hv).fillna(False).astype(bool)
    pts = pts.sort_values("price").reset_index(drop=True)

    # Clustering: every swing point is tried as the centre of a zone, and the zone
    # takes all points within cluster_atr x ATRd of it. Over a 500-day lookback the
    # price can be far from today's, so the width is a percentage of price (today's
    # ATRd / today's close) rather than dollars. Overlapping candidates are resolved
    # below by keeping the strongest.
    atr_pct = a / float(daily["close"].iloc[-1])
    width = np.log1p(p["cluster_atr"] * atr_pct)
    logp = np.log(pts["price"].to_numpy(float))
    clusters = {tuple(np.flatnonzero(np.abs(logp - x) <= width)) for x in logp}

    pos = {d: i for i, d in enumerate(daily.index)}
    hi_arr, lo_arr = daily["high"].to_numpy(), daily["low"].to_numpy()
    levels = []
    for idx in clusters:
        c = pts.loc[list(idx)].sort_values("date")
        level_px = float(c["price"].median())
        away = p["min_excursion_atr"] * atr_pct * level_px
        # A new touch counts only after price has moved at least `away` from the
        # level since the previous touch. Swings inside the same chop are one touch.
        touches, last = [], None
        for _, r in c.iterrows():
            if last is None:
                touches.append(r)
                last = r
                continue
            i0, i1 = pos[last["date"]], pos[r["date"]]
            if i1 - i0 <= p["swing_bars"]:
                continue
            seg = slice(i0 + 1, i1)
            moved = max(hi_arr[seg].max(initial=level_px) - level_px, level_px - lo_arr[seg].min(initial=level_px))
            if moved >= away:
                touches.append(r)
                last = r
        if len(touches) < p["min_touches"]:
            continue
        span = pos[touches[-1]["date"]] - pos[touches[0]["date"]]
        if span < p["min_span_days"]:
            continue
        pad = p["zone_pad_atr"] * atr_pct * level_px
        lo, hi = c["price"].min() - pad, c["price"].max() + pad
        months = span / 21
        hv_touches = int(c["high_vol"].sum())
        # [DEFAULT] strength = touches + 1 per 3 months of span (max 4) + 1 per high-volume touch (max 2)
        strength = len(touches) + min(4, int(months // 3)) + min(2, hv_touches)
        notes = f"{len(touches)} touches over {span}d"
        flip = _last_flip(daily, lo, hi, p["max_days_outside"])
        if flip:
            notes += f"; {flip}"
        levels.append(Level("pivot", lo, hi, strength, len(touches), touches[0]["date"], notes, ["pivot"]))
    # Within suppress_atr x ATRd only the strongest pivot is kept, so a choppy range
    # isn't tiled with overlapping zones.
    keep: list[Level] = []
    sup = p["suppress_atr"] * atr_pct
    for lv in sorted(levels, key=lambda x: (-x.strength, x.zone_high - x.zone_low, x.mid)):
        if all(abs(np.log(lv.mid / k.mid)) > sup for k in keep):
            keep.append(lv)
    return keep


def _last_flip(daily: pd.DataFrame, lo: float, hi: float, max_out: int) -> str:
    """Describe the most recent role flip: closes beyond the zone for more than
    `max_out` days in a row after being on the other side."""
    side = np.where(daily["close"] > hi, 1, np.where(daily["close"] < lo, -1, 0))
    run_side, run_len, prev_side, flip = 0, 0, 0, ""
    for d, s in zip(daily.index, side):
        if s == 0:
            continue
        if s == run_side:
            run_len += 1
        else:
            if run_len > max_out:
                prev_side = run_side
            run_side, run_len = s, 1
        if run_len == max_out + 1 and prev_side and prev_side != s:
            flip = f"{'support->resistance' if s < 0 else 'resistance->support'} {d:%Y-%m-%d}"
    return flip
