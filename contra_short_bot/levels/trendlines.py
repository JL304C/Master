"""Daily trendlines (spec 3.2), anchored on the most extreme swings.

Three kinds of overhead line are returned:
  * falling resistance through two major lower highs, still unbroken;
  * rising support through two major higher lows that price has broken (closed
    below for 2+ days), retested from below;
  * the upper rail of a channel: an intact rising support line shifted up to the
    highest high since its first anchor.
x is measured in trading days, so lines skip weekends and holidays.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from levels.model import Level


def zigzag(daily: pd.DataFrame, atr_d: pd.Series, mult: float) -> list[tuple[int, float, str]]:
    """Major swings: alternating highs and lows separated by a reversal of
    >= mult x ATRd (ATR as of the swing). Returns (bar index, price, 'H'|'L').
    The final, unconfirmed leg is dropped."""
    h, l, a = daily["high"].to_numpy(), daily["low"].to_numpy(), atr_d.to_numpy()
    start = int(np.argmax(~np.isnan(a)))
    swings: list[tuple[int, float, str]] = []
    trend, ext_i = 0, start
    hi_i, lo_i = start, start
    for i in range(start + 1, len(daily)):
        th = mult * a[i]
        if trend >= 0:
            if h[i] > h[hi_i]:
                hi_i = i
            if trend == 0 and l[i] < l[lo_i]:
                lo_i = i
            if h[hi_i] - l[i] >= th and (trend == 1 or hi_i > lo_i):
                swings.append((hi_i, h[hi_i], "H"))
                trend, lo_i = -1, i
                continue
        if trend <= 0:
            if l[i] < l[lo_i]:
                lo_i = i
            if trend == 0 and h[i] > h[hi_i]:
                hi_i = i
            if h[i] - l[lo_i] >= th and (trend == -1 or lo_i > hi_i):
                swings.append((lo_i, l[lo_i], "L"))
                trend, hi_i = 1, i
    return swings


def _value(i0: int, p0: float, i1: int, p1: float, x) -> np.ndarray:
    return p0 + (p1 - p0) * (np.asarray(x) - i0) / (i1 - i0)


def find_trendlines(daily: pd.DataFrame, atr_d: pd.Series, cfg: dict) -> list[Level]:
    t = cfg["levels"]["trendline"]
    a = float(atr_d.iloc[-1])
    n = len(daily)
    today = n  # the line's value is wanted for the session after the last bar
    h, l, c = daily["high"].to_numpy(), daily["low"].to_numpy(), daily["close"].to_numpy()
    sw = zigzag(daily, atr_d, t["zigzag_atr"])
    highs = [s for s in sw if s[2] == "H"]
    lows = [s for s in sw if s[2] == "L"]
    tol = t["touch_atr"] * a
    out: list[Level] = []

    def touches(i0, p0, i1, p1, prices, from_i):
        """Bars after the second anchor whose high (or low) came within tol of the line."""
        idx = np.arange(from_i, n)
        if not len(idx):
            return []
        v = _value(i0, p0, i1, p1, idx)
        hit = np.abs(prices[idx] - v) <= tol
        # Touches on consecutive days count once.
        res, last = [], -10
        for j in idx[hit]:
            if j - last > 3:
                res.append(j)
            last = j
        return res

    # Falling resistance: two major lower highs, no close above the line between them.
    for x, (i0, p0, _) in enumerate(highs):
        for i1, p1, _ in highs[x + 1:]:
            if p1 >= p0 or n - i1 > t["max_age_days"]:
                continue
            between = np.arange(i0 + 1, i1)
            if len(between) and (c[between] > _value(i0, p0, i1, p1, between)).any():
                continue
            after = np.arange(i1 + 1, n)
            line_after = _value(i0, p0, i1, p1, after)
            if len(after) and (c[after] > line_after + tol).sum() > 0:
                continue  # broken: price closed above the falling line
            tch = touches(i0, p0, i1, p1, h, i1 + 3)
            if not tch:
                continue
            out.append(_level("falling resistance", daily, i0, p0, i1, p1, today, a, t, 2 + len(tch)))

    # Rising support lines (two major higher lows, no close below between them).
    for x, (i0, p0, _) in enumerate(lows):
        for i1, p1, _ in lows[x + 1:]:
            if p1 <= p0 or n - i1 > t["max_age_days"]:
                continue
            between = np.arange(i0 + 1, i1)
            if len(between) and (c[between] < _value(i0, p0, i1, p1, between)).any():
                continue
            tch = touches(i0, p0, i1, p1, l, i1 + 3)
            after = np.arange(i1 + 1, n)
            below = c[after] < _value(i0, p0, i1, p1, after)
            broken_at = _first_run(below, t["broken_days"])
            if broken_at is not None:
                # A line validated by a third touch, then broken, is now overhead resistance (ICPT).
                if any(j < after[broken_at] for j in tch) and n - after[broken_at] <= t["broken_retest_days"]:
                    lv = _level("broken rising support", daily, i0, p0, i1, p1, today, a, t, 2 + len(tch))
                    lv.notes += f"; broke {daily.index[after[broken_at]]:%Y-%m-%d}"
                    out.append(lv)
            elif tch:
                # Intact support -> channel upper rail through the highest high since the first anchor.
                span = np.arange(i0, n)
                off = (h[span] - _value(i0, p0, i1, p1, span)).max()
                out.append(_level("channel rail", daily, i0, p0 + off, i1, p1 + off, today, a, t, 2 + len(tch)))
    out = [lv for lv in out if lv.zone_low > 0]
    # Keep only the best line of each type (most touches, then most recent second
    # anchor), so the same swings don't produce a fan of near-duplicate lines.
    best: dict[str, Level] = {}
    for lv in out:
        k = lv.notes.split(" 20")[0]
        if k not in best or (lv.touches, lv.line[5]) > (best[k].touches, best[k].line[5]):
            best[k] = lv
    return _dedupe(list(best.values()), a * t["zone_atr"] * 2)


def _first_run(mask: np.ndarray, k: int) -> int | None:
    run = 0
    for j, m in enumerate(mask):
        run = run + 1 if m else 0
        if run >= k:
            return j - k + 1
    return None


def _level(name, daily, i0, p0, i1, p1, today, a, t, touches) -> Level:
    """`touches` counts both anchors plus later touches; strength = touches [DEFAULT]."""
    v = float(_value(i0, p0, i1, p1, today))
    z = t["zone_atr"] * a
    lv = Level("trendline", v - z, v + z, float(touches), touches, daily.index[i0],
               f"{name} {daily.index[i0]:%Y-%m-%d}->{daily.index[i1]:%Y-%m-%d}",
               ["channel" if name == "channel rail" else "trendline"])
    # Anchors as (date, price) pairs plus their trading-day indices, for charting.
    lv.line = (daily.index[i0], float(p0), daily.index[i1], float(p1), i0, i1)
    return lv


def _dedupe(levels: list[Level], tol: float) -> list[Level]:
    """Keep the strongest line among those whose values today are within tol."""
    keep: list[Level] = []
    for lv in sorted(levels, key=lambda x: -x.strength):
        if all(abs(lv.mid - k.mid) > tol for k in keep):
            keep.append(lv)
    return keep
