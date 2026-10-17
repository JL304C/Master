"""Gaps (spec 3.3), moving averages, round numbers, prior/52-week highs and
manual levels (spec 3.4-3.5)."""
from __future__ import annotations

import math

import numpy as np

import pandas as pd
import yaml

from indicators.core import ema, sma
from levels.model import Level
from levels.trendlines import zigzag

# [DEFAULT] base strengths for structure levels that have no touch count.
STRENGTH = {"gap_fill": 3, "gap_entry": 2, "sma50": 1, "sma100": 1, "sma200": 2, "ema20": 1,
            "prior_high": 1, "prior_low": 1, "52w_high": 2, "52w_low": 1}


def find_gaps(daily: pd.DataFrame, atr_d: pd.Series, cfg: dict) -> list[Level]:
    """Unfilled down-gaps. Gap size is prior close -> gap-day open; a void must
    remain (gap-day high below the prior day's low). The gap fill edge is the
    pre-gap low; the gap entry edge is the highest high traded inside the void
    since. A daily close above the gap fill removes the gap."""
    g = cfg["levels"]["gap"]
    a_now = float(atr_d.iloc[-1])
    z = g["zone_atr"] * a_now
    o, h, l, c = (daily[k].to_numpy() for k in ("open", "high", "low", "close"))
    a = atr_d.to_numpy()
    out = []
    for i in range(1, len(daily)):
        size = c[i - 1] - o[i]
        if h[i] >= l[i - 1] or not (size >= g["min_atr"] * (a[i - 1] if a[i - 1] == a[i - 1] else 1e9)
                                    or size >= g["min_pct"] * c[i - 1]):
            continue
        fill = l[i - 1]
        later_close = c[i + 1:]
        if (later_close > fill).any():
            continue
        entry = h[i:].max()
        d = daily.index[i]
        note = f"down-gap {d:%Y-%m-%d} {size / c[i - 1]:.1%}"
        out.append(Level("gap_fill", fill - z, fill + z, STRENGTH["gap_fill"], 0, d, note, ["gap fill"]))
        if entry < fill - 2 * z:
            out.append(Level("gap_entry", entry - z, entry + z, STRENGTH["gap_entry"], 0, d, note, ["gap entry"]))
    return out


def find_swings(daily: pd.DataFrame, atr_d: pd.Series, cfg: dict) -> list[Level]:
    """Major swing highs and lows (the zigzag's >= 3 x ATRd reversals) as levels,
    even with only one or two touches: an old major high (DLTR's 2022 double top)
    or a single blow-off top (AAP April 2023) is a level a trader draws. Two major
    swings at the same price make a double top/bottom and add their strength. [ADDED]"""
    sc = cfg["levels"]["swing"]
    a = float(atr_d.iloc[-1])
    atr_pct = a / float(daily["close"].iloc[-1])
    zz = zigzag(daily, atr_d, cfg["levels"]["trendline"]["zigzag_atr"])
    out, used = [], set()
    for j, (i, px, t) in enumerate(zz):
        if j in used:
            continue
        twins = [k for k, (_, q, u) in enumerate(zz) if k != j and k not in used
                 and abs(np.log(q / px)) <= np.log1p(sc["twin_atr"] * atr_pct)]
        used.update([j, *twins])
        group = [zz[k] for k in [j, *twins]]
        prices = [g[1] for g in group]
        pad = sc["zone_atr"] * atr_pct * px
        kind = "double " if len(group) > 1 else ""
        name = f"{kind}{'top' if t == 'H' else 'bottom'}" if kind else f"swing {'high' if t == 'H' else 'low'}"
        dates = ", ".join(f"{daily.index[g[0]]:%Y-%m-%d}" for g in group)
        out.append(Level("swing", min(prices) - pad, max(prices) + pad, sc["strength"] * len(group), len(group),
                         daily.index[min(g[0] for g in group)], f"{name} {dates}", [name]))
    return out


def find_mas(daily: pd.DataFrame, atr_d: pd.Series, cfg: dict) -> list[Level]:
    m = cfg["levels"]["ma"]
    z = m["zone_atr"] * float(atr_d.iloc[-1])
    out = []
    for n in m["sma"]:
        v = sma(daily["close"], n).iloc[-1]
        if v == v:
            out.append(Level("sma", v - z, v + z, STRENGTH.get(f"sma{n}", 1), 0, None, "", [f"{n}SMA"]))
    for n in m["ema"]:
        v = ema(daily["close"], n).iloc[-1]
        if v == v:
            out.append(Level("sma", v - z, v + z, STRENGTH.get(f"ema{n}", 1), 0, None, "", [f"{n}EMA"]))
    return out


def psych_step(price: float) -> float:
    return 50.0 if price > 200 else 10.0 if price >= 50 else 5.0


def find_psych(daily: pd.DataFrame, atr_d: pd.Series, cfg: dict, span_atr: float = 4.0) -> list[Level]:
    """Round numbers within span_atr x ATRd of the last close."""
    p = cfg["levels"]["psych"]
    a = float(atr_d.iloc[-1])
    last = float(daily["close"].iloc[-1])
    step = psych_step(last)
    z = p["zone_atr"] * a
    out = []
    k = math.floor((last - span_atr * a) / step)
    while k * step <= last + span_atr * a:
        v = k * step
        if v > 0 and abs(v - last) <= span_atr * a:
            out.append(Level("psych", v - z, v + z, p["strength"], 0, None, "", ["psych"]))
        k += 1
    return out


def find_prior(daily: pd.DataFrame, atr_d: pd.Series, cfg: dict) -> list[Level]:
    z = 0.1 * float(atr_d.iloc[-1])
    last = daily.iloc[-1]
    yr = daily.iloc[-252:]
    rows = [("prior_high", last["high"], "prior day high"), ("prior_low", last["low"], "prior day low"),
            ("52w_high", yr["high"].max(), "52w high"), ("52w_low", yr["low"].min(), "52w low")]
    return [Level("prior_high", v - z, v + z, STRENGTH[k], 0, None, "", [lab]) for k, v, lab in rows]


def _parse_price(x) -> tuple[float, float]:
    if isinstance(x, str) and "-" in x:
        lo, hi = (float(p) for p in x.split("-", 1))
        return lo, hi
    return float(x), float(x)


def manual_levels(path, symbol: str, day: pd.Timestamp, atr_d: float, cfg: dict) -> list[Level]:
    """Jeff's levels for this symbol and day from manual_levels.yaml (spec 3.5)."""
    try:
        data = yaml.safe_load(open(path)) or {}
    except FileNotFoundError:
        return []
    entry = data.get(day.strftime("%Y-%m-%d")) or data.get(day.date()) or {}
    sym = entry.get(symbol) or {}
    out = []
    pad = 0.1 * atr_d
    for side in ("resistance", "support"):
        for x in sym.get(side, []) or []:
            lo, hi = _parse_price(x)
            out.append(Level("manual", lo - pad, hi + pad, cfg["levels"]["manual_strength"], 0, day,
                             f"manual {side}", ["manual"]))
    return out
