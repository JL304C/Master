"""Market tone (spec 6.1): is the overall market strong or weak?

Premarket tone scores each index ETF on gap, location vs the prior day's range
and premarket trend, then takes the weighted average. Live tone compares each
ETF's last price with its own session VWAP. Both use only bars completed before
`asof`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time

import pandas as pd

from data.bars import RTH_START, day_minutes
from data.fetch import load_config
from indicators.core import session_vwap
from selection.context import daily_context


@dataclass
class Tone:
    label: str
    score: float
    divergence: bool = False
    parts: dict = field(default_factory=dict)   # symbol -> score details

    def __str__(self) -> str:
        div = " (QQQ/IWM divergence -> NEUTRAL)" if self.divergence else ""
        return f"{self.label} {self.score:+.2f}{div}"


def _t(s: str) -> time:
    return time.fromisoformat(s)


def label_for(score: float, cfg: dict) -> str:
    L = cfg["selection"]["tone"]["labels"]
    if score <= L["weak"]:
        return "WEAK"
    if score <= L["soft"]:
        return "SOFT"
    if score < L["firm"]:
        return "NEUTRAL"
    if score < L["strong"]:
        return "FIRM"
    return "STRONG"


def proxy_premarket_score(symbol: str, day, asof: time, cfg: dict) -> dict | None:
    t = cfg["selection"]["tone"]
    ctx = daily_context(symbol, day)
    pm = day_minutes(symbol, day, _t(t["premarket_from"]), asof)
    pm = pm[pm["volume"] > 0]
    if pm.empty:
        return None
    last = float(pm["close"].iloc[-1])
    gap = (last - ctx.prev_close) / ctx.atr_d
    g = 1 if gap > t["gap_atr"] else -1 if gap < -t["gap_atr"] else 0
    loc = 1 if last > ctx.prev_high else -1 if last < ctx.prev_low else 0
    tp = (pm["high"] + pm["low"] + pm["close"]) / 3
    vwap = float((tp * pm["volume"]).sum() / pm["volume"].sum())
    tr = t["trend_points"] * (1 if last > vwap else -1 if last < vwap else 0)
    return {"last": last, "gap_atr": round(gap, 2), "gap": g, "location": loc, "trend": tr, "score": g + loc + tr}


def premarket_tone(day, asof: time | None = None, cfg: dict | None = None) -> Tone:
    cfg = cfg or load_config()
    asof = asof or _t(cfg["selection"]["tone"]["final_at"])
    weights = cfg["data"]["index_proxies"]
    parts = {s: proxy_premarket_score(s, day, asof, cfg) for s in weights}
    return _combine(parts, weights, cfg)


def live_tone(day, asof: time, cfg: dict | None = None) -> Tone:
    """Each proxy scores +1 above its session VWAP, -1 below (spec 6.1, intraday update)."""
    cfg = cfg or load_config()
    weights = cfg["data"]["index_proxies"]
    parts = {}
    for s in weights:
        m = day_minutes(s, day, RTH_START, asof)
        if m.empty:
            parts[s] = None
            continue
        vw = float(session_vwap(m).iloc[-1])
        last = float(m["close"].iloc[-1])
        parts[s] = {"last": last, "vwap": round(vw, 2), "score": 1 if last > vw else -1 if last < vw else 0}
    return _combine(parts, weights, cfg)


def _combine(parts: dict, weights: dict, cfg: dict) -> Tone:
    have = {s: p for s, p in parts.items() if p is not None}
    if not have:
        return Tone("NEUTRAL", 0.0, False, parts)
    score = sum(weights[s] * p["score"] for s, p in have.items()) / sum(weights[s] for s in have)
    q, i = have.get("QQQ"), have.get("IWM")
    div = bool(q and i and q["score"] * i["score"] < 0)
    return Tone("NEUTRAL" if div else label_for(score, cfg), round(score, 3), div, parts)


def policy(tone_label: str, cfg: dict | None = None) -> dict:
    """What the tone allows (spec 6.1 table): fade (A/B/C) and trend (D) permissions,
    extra requirements and the size multiplier."""
    cfg = cfg or load_config()
    return cfg["selection"]["tone_policy"][tone_label]
