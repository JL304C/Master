"""Load cached 1-minute bars, build daily and N-minute bars, apply split adjustments."""
from __future__ import annotations

from datetime import time
from functools import lru_cache
from pathlib import Path

import pandas as pd
import yaml

from data.fetch import ET, ROOT, cache_path, load_config

RTH_START, RTH_END = time(9, 30), time(16, 0)
PRE_START = time(4, 0)


def load_minutes(symbol: str, dataset: str | None = None, cfg: dict | None = None) -> pd.DataFrame:
    cfg = cfg or load_config()
    dataset = dataset or cfg["data"]["intraday_dataset"]
    return pd.read_parquet(cache_path(cfg, dataset, symbol))


def rth(df: pd.DataFrame) -> pd.DataFrame:
    t = df.index.time
    return df[(t >= RTH_START) & (t < RTH_END)]


def premarket(df: pd.DataFrame) -> pd.DataFrame:
    t = df.index.time
    return df[(t >= PRE_START) & (t < RTH_START)]


def daily_from_minutes(df: pd.DataFrame) -> pd.DataFrame:
    """Regular-session daily bars, indexed by session date (tz-naive)."""
    m = rth(df)
    g = m.groupby(m.index.date)
    out = pd.DataFrame({
        "open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
        "close": g["close"].last(), "volume": g["volume"].sum(),
    })
    out.index = pd.DatetimeIndex(out.index, name="date")
    # Drop sessions with almost no trading (data glitches, single prints).
    return out[g.size().values >= 30]


def resample(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Aggregate 1-minute bars to N-minute bars aligned to the 09:30 open, within each session."""
    if minutes == 1:
        return df
    origin = "start_day"
    off = pd.Timedelta(minutes=(9 * 60 + 30) % minutes)
    r = df.resample(f"{minutes}min", origin=origin, offset=off, label="left", closed="left")
    out = r.agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    return out.dropna(subset=["open"])


@lru_cache(maxsize=1)
def _splits() -> dict:
    p = ROOT / "splits.yaml"
    return yaml.safe_load(p.read_text()) or {} if p.exists() else {}


def splits_for(symbol: str) -> list[tuple[pd.Timestamp, float]]:
    return [(pd.Timestamp(s["date"]), float(s["ratio"])) for s in _splits().get(symbol, [])]


def adjust_daily(daily: pd.DataFrame, symbol: str, asof: pd.Timestamp) -> pd.DataFrame:
    """Put every bar on the share basis in force on `asof`, using only splits with ex-date <= asof."""
    out = daily.copy()
    for ex, ratio in splits_for(symbol):
        if ex <= asof:
            before = out.index < ex
            out.loc[before, ["open", "high", "low", "close"]] /= ratio
            out.loc[before, "volume"] = (out.loc[before, "volume"] * ratio).round()
    return out


@lru_cache(maxsize=64)
def daily_bars(symbol: str) -> pd.DataFrame:
    """Unadjusted regular-session daily bars from the long-history dataset, cached to disk."""
    cfg = load_config()
    src = cache_path(cfg, cfg["data"]["daily_dataset"], symbol)
    dst = Path(str(src).replace(".parquet", ".daily.parquet"))
    if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
        return pd.read_parquet(dst)
    d = daily_from_minutes(pd.read_parquet(src))
    d.to_parquet(dst)
    return d


@lru_cache(maxsize=48)
def minutes(symbol: str) -> pd.DataFrame:
    """All cached 1-minute bars for a symbol (premarket, regular and after hours)."""
    return load_minutes(symbol)


def sessions(symbol: str) -> pd.DatetimeIndex:
    """Dates with a regular session, from the daily bars."""
    return daily_bars(symbol).index


def day_minutes(symbol: str, day, start: time = PRE_START, end: time = RTH_END) -> pd.DataFrame:
    """One day's 1-minute bars between `start` and `end` (default 04:00-16:00 ET)."""
    df = minutes(symbol)
    day = pd.Timestamp(day).normalize()
    a = pd.Timestamp.combine(day.date(), start).tz_localize(ET)
    b = pd.Timestamp.combine(day.date(), end).tz_localize(ET)
    i, j = df.index.searchsorted(a), df.index.searchsorted(b)
    return df.iloc[i:j]


def rth_window(symbol: str, day, prior_sessions: int) -> pd.DataFrame:
    """Regular-session 1-minute bars for `day` plus the `prior_sessions` before it,
    so intraday indicators (ATR, Bollinger bands) are warmed up at the open."""
    s = sessions(symbol)
    day = pd.Timestamp(day).normalize()
    k = s.searchsorted(day)
    first = s[max(0, k - prior_sessions)]
    df = minutes(symbol)
    a = pd.Timestamp.combine(first.date(), RTH_START).tz_localize(ET)
    b = pd.Timestamp.combine(day.date(), RTH_END).tz_localize(ET)
    out = rth(df.iloc[df.index.searchsorted(a):df.index.searchsorted(b)])
    # Put warm-up days from before a split on `day`'s share basis.
    for ex, ratio in splits_for(symbol):
        if first < ex <= day:
            before = out.index < pd.Timestamp(ex).tz_localize(ET)
            if before.any():
                out = out.copy()
                out.loc[before, ["open", "high", "low", "close"]] /= ratio
                out.loc[before, "volume"] = (out.loc[before, "volume"] * ratio).round()
    return out


@lru_cache(maxsize=1)
def _summary() -> pd.DataFrame | None:
    cfg = load_config()
    p = ROOT / cfg["data"]["cache_dir"] / cfg["data"]["summary_dataset"] / "daily.parquet"
    return pd.read_parquet(p) if p.exists() else None


@lru_cache(maxsize=64)
def nasdaq_share(symbol: str) -> float:
    """Median share of the official daily volume that trades on Nasdaq (the
    intraday data's venue). Share-count thresholds written for the full tape are
    multiplied by this before being compared with our volumes."""
    s = _summary()
    default = load_config()["data"]["default_nasdaq_share"]
    if s is None or symbol not in set(s["symbol"]):
        return default
    full = s.loc[s["symbol"] == symbol, "volume"]
    ours = daily_bars(symbol)["volume"].reindex(full.index).dropna()
    r = (ours / full.reindex(ours.index)).replace([float("inf")], float("nan")).dropna()
    return float(r.median()) if len(r) >= 20 else default
