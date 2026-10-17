"""Per-minute features the strategies read: the scanner columns plus candle,
volume and 2-/5-minute values. Row t only uses bars completed by t + 1 minute."""
from __future__ import annotations

import numpy as np
import pandas as pd

from data.bars import resample, rth_window
from data.fetch import load_config
from indicators.core import atr, ema
from selection.scanners import at_completion, intraday_scan


def build_features(symbol: str, day, cfg: dict | None = None, spy_pct: pd.Series | None = None) -> pd.DataFrame:
    cfg = cfg or load_config()
    day = pd.Timestamp(day).normalize()
    d = intraday_scan(symbol, day, cfg, spy_pct=spy_pct)
    if d.empty:
        return d
    idx = d.index
    w = rth_window(symbol, day, cfg["selection"]["rvol_lookback_days"])

    # Candles.
    d["body"] = (d["close"] - d["open"]).abs()
    d["avg_body"] = d["body"].expanding().mean().shift(1)
    d["wide"] = d["body"] >= cfg["engine"]["extension"]["wide_body_mult"] * d["avg_body"]
    d["red"] = d["close"] < d["open"]
    d["lower_half"] = (d["close"] - d["low"]) <= cfg["engine"]["rejection_close_frac"] * (d["high"] - d["low"])
    hh = (d["high"] > d["high"].shift(1)).astype(int)
    d["hh_run"] = hh.groupby((hh == 0).cumsum()).cumsum()          # consecutive higher highs ending here
    d["hh_run_prev"] = d["hh_run"].shift(1).fillna(0)

    # Volume.
    d["hod_vol_1m"] = d["volume"] >= d["volume"].cummax()
    up = (d["volume"] > d["volume"].shift(1)).astype(int)
    d["vol_ramp"] = up.groupby((up == 0).cumsum()).cumsum() >= 3   # 4 bars of rising volume = 3 rises
    b5 = resample(w, 5)
    b5 = b5[b5["volume"] > 0]
    b5_today = b5[b5.index.date == day.date()]
    hod5 = b5_today["volume"] >= b5_today["volume"].cummax()
    d["hod_vol_5m"] = at_completion(hod5.astype(float), idx).fillna(0).astype(bool)

    # Speed: rise from the lowest low of the last N minutes.
    n = cfg["engine"]["extension"]["speed_minutes"]
    d["rise_n"] = d["high"] - d["low"].rolling(n, min_periods=1).min()

    # 2-minute EMA9 alignment (State 5) and 2-minute higher highs (disconnect rule).
    b2 = resample(w, 2)
    b2 = b2[b2["volume"] > 0]
    e9 = ema(b2["close"], 9)
    below = (b2["close"] < e9).astype(float)
    m2hh = (b2["high"] > b2["high"].shift(1)).astype(float)
    d["m2_below_ema9"] = _done(below, idx, 2).fillna(0).astype(bool)
    d["m2_higher_high"] = _done(m2hh, idx, 2).fillna(0).astype(bool)

    # 5-minute bars for Strategy D: values of the last completed bar, and a flag on
    # the minute that completes a 5-minute bar.
    e20 = ema(b5["close"], 20)
    for col, s in {"m5_open": b5["open"], "m5_high": b5["high"], "m5_low": b5["low"], "m5_close": b5["close"],
                   "m5_ema20": e20}.items():
        d[col] = at_completion(s, idx)
    d["m5_done"] = ((idx + pd.Timedelta(minutes=1)).minute % 5 == 0)
    return d


def _done(s: pd.Series, minute_index: pd.DatetimeIndex, minutes: int) -> pd.Series:
    """Value of the last completed N-minute bar at the close of each 1-minute bar."""
    x = s.copy()
    x.index = x.index + pd.Timedelta(minutes=minutes)
    out = x.reindex(minute_index + pd.Timedelta(minutes=1), method="ffill")
    out.index = minute_index
    return out
