"""Scanners (spec 6.2), rebuilt from the Warrior Scan Layout (Aug 2017) and the
Primus filter screenshots.

Premarket scans look at one moment (`premarket_scan`). Intraday scans are
computed for every 1-minute bar of the session at once (`intraday_scan`); each
row only uses bars completed by the end of that minute, so a backtest can read
the row for bar t at t + 1 minute without lookahead.
"""
from __future__ import annotations

from datetime import time

import numpy as np
import pandas as pd

from data.bars import RTH_START, day_minutes, nasdaq_share, resample, rth_window
from data.fetch import load_config
from indicators.core import atr, bollinger_lower, bollinger_upper, ema, rsi, session_vwap, sma
from selection.context import daily_context, has_history

ONE = pd.Timedelta(minutes=1)


def _t(s: str) -> time:
    return time.fromisoformat(s)


# ------------------------------------------------------------------ premarket

def premarket_scan(symbol: str, day, asof: time, cfg: dict | None = None) -> dict:
    """Pre Market Bid Up / Ask Down 3.5%, new premarket highs/lows, premarket RVOL
    [DOCUMENTED]. Last trade price stands in for the bid."""
    cfg = cfg or load_config()
    p = cfg["selection"]["premarket"]
    ctx = daily_context(symbol, day)
    pm = day_minutes(symbol, day, _t(p["from"]), asof)
    pm = pm[pm["volume"] > 0]
    out = {"symbol": symbol, "prev_close": ctx.prev_close, "atr_d": ctx.atr_d, "pm_last": np.nan, "pm_volume": 0,
           "gap_pct": 0.0, "bid_up": False, "ask_down": False, "pm_new_high": False, "pm_new_low": False,
           "pm_rvol": np.nan}
    if pm.empty:
        return out
    last = float(pm["close"].iloc[-1])
    vol = int(pm["volume"].sum())
    min_vol = p["min_volume"] * nasdaq_share(symbol)
    chg = last / ctx.prev_close - 1
    recent = pm.index >= pm.index[-1] - pd.Timedelta(minutes=p["new_high_lookback_min"] - 1)
    out.update(pm_last=last, pm_volume=vol, pm_high=float(pm["high"].max()), pm_low=float(pm["low"].min()),
               gap_pct=chg, bid_up=bool(chg > p["move_pct"] and vol > min_vol),
               ask_down=bool(chg < -p["move_pct"] and vol > min_vol))
    on_list = out["bid_up"] or out["ask_down"]
    out["pm_new_high"] = bool(on_list and pm.loc[recent, "high"].max() >= out["pm_high"])
    out["pm_new_low"] = bool(on_list and pm.loc[recent, "low"].min() <= out["pm_low"])
    out["pm_rvol"] = _premarket_rvol(symbol, day, _t(p["from"]), asof, vol, cfg)
    return out


def _premarket_rvol(symbol, day, start, asof, vol, cfg) -> float:
    ctx = daily_context(symbol, day)
    past = ctx.hist.index[-cfg["selection"]["rvol_lookback_days"]:]
    vols = [day_minutes(symbol, d, start, asof)["volume"].sum() for d in past]
    avg = float(np.mean(vols)) if vols else 0.0
    return vol / avg if avg > 0 else np.nan


def open_scan(symbol: str, day, cfg: dict | None = None) -> dict:
    """Gap Up / Gap Down Static at 09:30 [DOCUMENTED]."""
    cfg = cfg or load_config()
    ctx = daily_context(symbol, day)
    m = day_minutes(symbol, day, RTH_START, time(9, 31))
    if m.empty:
        return {"open": np.nan, "gap_up": False, "gap_down": False, "gap": 0.0}
    o = float(m["open"].iloc[0])
    g = cfg["selection"]["gap_static_pct"]
    return {"open": o, "gap": o - ctx.prev_close,
            "gap_up": bool(o / ctx.prev_close >= 1 + g and o > ctx.prev_high),
            "gap_down": bool(o / ctx.prev_close <= 1 - g and o < ctx.prev_low)}


# ------------------------------------------------------------------ intraday

def _at_completion(s5: pd.Series, minute_index: pd.DatetimeIndex) -> pd.Series:
    """Value of the last *completed* 5-minute bar at the close of each 1-minute bar.
    A 5-minute bar labelled L completes at L + 5 min; a 1-minute bar labelled t at t + 1 min."""
    done = s5.copy()
    done.index = done.index + pd.Timedelta(minutes=5)
    out = done.reindex(minute_index + ONE, method="ffill")
    out.index = minute_index
    return out


def _rvol_so_far(window: pd.DataFrame, today) -> pd.Series:
    """Cumulative volume today / average cumulative volume at the same minute of
    the prior sessions in `window`."""
    dates = window.index.date
    minute_of_day = (window.index.hour * 60 + window.index.minute) - (9 * 60 + 30)
    cum = window["volume"].groupby(dates).cumsum()
    frame = pd.DataFrame({"d": dates, "m": minute_of_day, "cum": cum.to_numpy()})
    past = frame[frame["d"] != today]
    prof = past.groupby("m")["cum"].mean()
    t = frame[frame["d"] == today]
    base = prof.reindex(t["m"]).to_numpy()
    return pd.Series(t["cum"].to_numpy() / np.where(base > 0, base, np.nan), index=window.index[dates == today])


def intraday_scan(symbol: str, day, cfg: dict | None = None, spy_pct: pd.Series | None = None) -> pd.DataFrame:
    """One row per regular-session minute of `day` with indicators and scan flags."""
    cfg = cfg or load_config()
    S = cfg["selection"]
    day = pd.Timestamp(day).normalize()
    today = day.date()
    if not has_history(symbol, day):
        return pd.DataFrame()
    ctx = daily_context(symbol, day)
    w = rth_window(symbol, day, S["rvol_lookback_days"])
    if w.empty or w.index[-1].date() != today:
        return pd.DataFrame()

    # Indicators on the continuous multi-day series (warm at the open).
    atr1 = atr(w, 14)
    sma20 = sma(w["close"], S["quickstrike"]["bb_period"])
    bbu1 = bollinger_upper(w["close"], S["quickstrike"]["bb_period"], S["quickstrike"]["bb_k"])
    bbl1 = bollinger_lower(w["close"], S["quickstrike"]["bb_period"], S["quickstrike"]["bb_k"])
    ema20 = ema(w["close"], 20)
    rsi1 = rsi(w["close"], 14)
    b5 = resample(w, 5)
    b5 = b5[b5["volume"] > 0]
    atr5 = atr(b5, 14)
    bbu5 = bollinger_upper(b5["close"], S["quickstrike"]["bb_period"], S["quickstrike"]["bb_k"])
    bbl5 = bollinger_lower(b5["close"], S["quickstrike"]["bb_period"], S["quickstrike"]["bb_k"])
    vol5_ratio = b5["volume"] / b5["volume"].rolling(50, min_periods=20).mean().shift(1)

    is_today = w.index.date == today
    d = w[is_today].copy()
    idx = d.index
    d["atr1"] = atr1[is_today]
    d["atr5"] = _at_completion(atr5, idx)
    d["vwap"] = session_vwap(d)
    d["vdif"] = d["close"] - d["vwap"]
    d["rvol"] = _rvol_so_far(w, today)
    d["sma20"], d["ema20"], d["rsi"] = sma20[is_today], ema20[is_today], rsi1[is_today]
    d["vol5_ratio"] = _at_completion(vol5_ratio, idx)
    after = idx.time >= _t(S["contra"]["start"])

    # New highs/lows of the day (1-minute) and of completed 5-minute bars.
    prior_hi = d["high"].cummax().shift(1)
    prior_lo = d["low"].cummin().shift(1)
    d["new_day_high"] = d["high"] > prior_hi
    d["new_day_low"] = d["low"] < prior_lo
    b5t = b5[b5.index.date == today]
    nh5 = b5t["high"] > b5t["high"].cummax().shift(1)
    nl5 = b5t["low"] < b5t["low"].cummin().shift(1)
    done = idx + ONE
    nh5_done = set(nh5[nh5].index + pd.Timedelta(minutes=5))
    nl5_done = set(nl5[nl5].index + pd.Timedelta(minutes=5))
    d["new_5m_high"] = [t in nh5_done for t in done]
    d["new_5m_low"] = [t in nl5_done for t in done]

    # Contra Short: new high of day and stretched above VWAP [DOCUMENTED rule;
    # tiers 2-3 INFERRED; normalized mode ADDED]. Contra Long is the mirror.
    c = S["contra"]
    trig_hi = d["new_day_high"] if c["trigger"] == "new_day_high" else d["new_5m_high"]
    trig_lo = d["new_day_low"] if c["trigger"] == "new_day_high" else d["new_5m_low"]
    in_play = after & (d["rvol"] >= c["min_rvol"])
    d["contra_short_dollar"] = _tier(d["vdif"], c["dollar_tiers"]) * (trig_hi & in_play)
    d["contra_short_norm"] = (_tier(d["vdif"] / d["atr5"], c["atr5_tiers"])
                              * ((d["vdif"] / d["close"]) >= c["min_pct_of_price"]) * (trig_hi & in_play))
    d["contra_long_dollar"] = _tier(-d["vdif"], c["dollar_tiers"]) * (trig_lo & in_play)
    d["contra_long_norm"] = (_tier(-d["vdif"] / d["atr5"], c["atr5_tiers"])
                             * ((-d["vdif"] / d["close"]) >= c["min_pct_of_price"]) * (trig_lo & in_play))
    pick = {"dollar": ["contra_short_dollar"], "normalized": ["contra_short_norm"],
            "both": ["contra_short_dollar", "contra_short_norm"]}[c["mode"]]
    d["contra_short"] = d[pick].min(axis=1).astype(int)
    d["contra_long"] = d[[x.replace("short", "long") for x in pick]].min(axis=1).astype(int)

    # Quickstrike High / Low [DOCUMENTED; normalized $0.50 ADDED].
    q = S["quickstrike"]
    sma_dif = d["close"] - d["sma20"]
    need = q["sma_dif_atr1"] * d["atr1"] if q["normalized"] else q["dollar_sma_dif"]
    vol_ok = d["vol5_ratio"] >= q["vol5_mult"]
    d["quickstrike_high"] = ((d["close"] > bbu1[is_today]) & (d["close"] > _at_completion(bbu5, idx))
                             & (sma_dif > need) & vol_ok)
    d["quickstrike_low"] = ((d["close"] < bbl1[is_today]) & (d["close"] < _at_completion(bbl5, idx))
                            & (-sma_dif > need) & vol_ok)

    # SP Top Reversal [PARTIAL; thresholds INFERRED].
    r = S["sp_top_reversal"]
    d["sp_top_reversal"] = ((d["rsi"] > r["rsi"]) & (d["vol5_ratio"] > r["vol5_mult"])
                            & (d["close"] - d["ema20"] >= r["ema20_dist_atr1"] * d["atr1"]))

    # Para Up [INFERRED]: >= 4 higher closes in a row, range expanding, total >= 3 x ATR1.
    pu = S["para_up"]
    up = (d["close"] > d["close"].shift(1)).astype(int)
    run = up.groupby((up == 0).cumsum()).cumsum()
    rng = d["high"] - d["low"]
    d["para_up"] = ((run >= pu["bars"]) & (rng > rng.shift(pu["bars"] - 1))
                    & (d["close"] - d["close"].shift(pu["bars"]) >= pu["move_atr1"] * d["atr1"]))

    # Consecutive Candles 5 Up [INFERRED: 5-minute candles].
    green5 = (b5["close"] > b5["open"]).astype(int)
    run5 = green5.groupby((green5 == 0).cumsum()).cumsum()
    d["cons_up"] = _at_completion(run5, idx).fillna(0) >= S["cons_up_bars"]

    # Gap Up and Go Up / Gap Down and Go Down [DOCUMENTED].
    g = open_scan(symbol, day, cfg)
    first5 = d[idx < idx[0] + pd.Timedelta(minutes=5)]
    after5 = idx >= idx[0] + pd.Timedelta(minutes=5)
    d["gap_up_go_up"] = g["gap_up"] & after5 & (d["high"] > first5["high"].max()) & d["new_day_high"]
    d["gap_down_go_down"] = g["gap_down"] & after5 & (d["low"] < first5["low"].min()) & d["new_day_low"]
    d["gap_reversal"] = g["gap_up"] & (d["close"] < g["open"])   # ChgOpen turned negative

    # Trendsell [DOCUMENTED]: daily setup, then the first break below yesterday's low.
    brk = d["low"] < ctx.prev_low
    d["trendsell"] = ctx.trendsell_setup() & brk & ~brk.shift(1, fill_value=False).cummax()

    # Relative weakness vs SPY [DEFAULT].
    d["pct_chg"] = d["close"] / ctx.prev_close - 1
    if spy_pct is not None:
        rw = S["rel_weak"]
        d["rel_weak"] = ((d["pct_chg"] - spy_pct.reindex(idx).ffill()) <= rw["vs_spy_pct"]) & (d["rvol"] >= rw["min_rvol"])
    else:
        d["rel_weak"] = False
    return d


def _tier(x: pd.Series, tiers: list[float]) -> pd.Series:
    """0 below the first threshold, else the highest tier number reached (1-3)."""
    t = pd.Series(0, index=x.index)
    for k, th in enumerate(tiers, 1):
        t[x > th] = k
    return t


def spy_pct_change(day, cfg: dict | None = None) -> pd.Series:
    ctx = daily_context("SPY", day)
    m = day_minutes("SPY", day, RTH_START)
    return m["close"] / ctx.prev_close - 1


SCAN_COLUMNS = ["contra_short", "contra_long", "quickstrike_high", "quickstrike_low", "sp_top_reversal", "para_up",
                "cons_up", "gap_up_go_up", "gap_down_go_down", "gap_reversal", "trendsell", "rel_weak"]


# Conditions that stay true for many minutes; logged only when they switch on.
STATE_SCANS = {"gap_reversal", "rel_weak", "cons_up"}


def hits(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Long-format log of every scan print: time, symbol, scan, tier/detail."""
    rows = []
    for col in SCAN_COLUMNS:
        if col not in df:
            continue
        s = df[col].astype(int) if col.startswith("contra") else df[col].astype(bool)
        if col in STATE_SCANS:
            s = s & ~s.shift(1, fill_value=False)   # log when the condition switches on
        on = s[s.astype(bool)]
        for t, v in on.items():
            rows.append({"time": t, "symbol": symbol, "scan": col, "tier": int(v) if col.startswith("contra") else "",
                         "price": round(float(df.at[t, "close"]), 2), "vdif": round(float(df.at[t, "vdif"]), 2),
                         "rvol": round(float(df.at[t, "rvol"]), 2) if pd.notna(df.at[t, "rvol"]) else np.nan})
    return pd.DataFrame(rows)
