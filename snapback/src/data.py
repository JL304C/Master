"""Databento download (with cost check and local cache) and bar resampling.

Talks to Databento's historical HTTP API directly so it works either with
DATABENTO_API_KEY in the environment or with a network secret that the
session's proxy attaches to requests for hist.databento.com.
"""
from __future__ import annotations

import io
import os
from pathlib import Path

import pandas as pd
import requests

API = "https://hist.databento.com/v0"
ET = "America/New_York"


def _auth():
    key = os.environ.get("DATABENTO_API_KEY")
    return (key, "") if key else None


def _year_chunks(start: str, end: str):
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    for year in range(s.year, e.year + 1):
        cs = max(s, pd.Timestamp(f"{year}-01-01"))
        ce = min(e, pd.Timestamp(f"{year + 1}-01-01"))
        if cs < ce:
            yield year, cs.strftime("%Y-%m-%d"), ce.strftime("%Y-%m-%d")


def _cache_path(cache_dir: Path, symbol: str, schema: str, year: int) -> Path:
    return cache_dir / f"{symbol.replace('.', '_')}_{schema}_{year}.parquet"


def estimate_cost(symbols: list[str], dcfg: dict, cache_dir: Path) -> float:
    """Dollar cost of the chunks not already cached."""
    total = 0.0
    for sym in symbols:
        for year, cs, ce in _year_chunks(dcfg["start"], dcfg["end"]):
            if _cache_path(cache_dir, sym, dcfg["schema"], year).exists():
                continue
            r = requests.get(f"{API}/metadata.get_cost", auth=_auth(), timeout=60, params={
                "dataset": dcfg["dataset"], "symbols": sym, "schema": dcfg["schema"],
                "start": cs, "end": ce, "stype_in": dcfg["stype_in"], "mode": "historical-streaming",
            })
            r.raise_for_status()
            cost = float(r.json())
            print(f"  {sym} {year}: ${cost:,.2f}")
            total += cost
    return total


def download(symbol: str, dcfg: dict, cache_dir: Path) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    for year, cs, ce in _year_chunks(dcfg["start"], dcfg["end"]):
        path = _cache_path(cache_dir, symbol, dcfg["schema"], year)
        if path.exists():
            continue
        r = requests.post(f"{API}/timeseries.get_range", auth=_auth(), timeout=600, data={
            "dataset": dcfg["dataset"], "symbols": symbol, "schema": dcfg["schema"],
            "start": cs, "end": ce, "stype_in": dcfg["stype_in"], "encoding": "csv",
            "pretty_px": "true", "pretty_ts": "true", "map_symbols": "true", "compression": "none",
        })
        r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text))
        df.to_parquet(path)
        print(f"  cached {path.name}: {len(df):,} rows")


def load_1m(symbol: str, dcfg: dict, cache_dir: Path) -> pd.DataFrame:
    frames = []
    for year, _, _ in _year_chunks(dcfg["start"], dcfg["end"]):
        path = _cache_path(cache_dir, symbol, dcfg["schema"], year)
        if path.exists():
            frames.append(pd.read_parquet(path))
    if not frames:
        raise FileNotFoundError(f"No cached data for {symbol} in {cache_dir}; run with --download")
    df = pd.concat(frames, ignore_index=True)
    df["ts"] = pd.to_datetime(df["ts_event"], utc=True).dt.tz_convert(ET)
    df = df.drop_duplicates("ts").set_index("ts").sort_index()
    cols = ["open", "high", "low", "close", "volume"]
    if "instrument_id" in df:
        cols.append("instrument_id")
    return df[cols]


def resample(bars: pd.DataFrame, rule: str, offset: str | None = None) -> pd.DataFrame:
    """OHLCV resample, bars labelled by their start time. Empty periods dropped."""
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    if "instrument_id" in bars:
        agg["instrument_id"] = "last"
    out = bars.resample(rule, label="left", closed="left", offset=offset).agg(agg)
    return out.dropna(subset=["open"])


def load_csv(path: str | Path) -> pd.DataFrame:
    """Load any OHLC CSV with a timestamp column (for other vendors or test fixtures)."""
    df = pd.read_csv(path)
    tcol = next(c for c in df.columns if c.lower() in ("ts", "ts_event", "timestamp", "time", "datetime", "date"))
    ts = pd.to_datetime(df[tcol], utc=True)
    df = df.drop(columns=[tcol])
    df.index = ts.dt.tz_convert(ET)
    df.columns = [c.lower() for c in df.columns]
    return df.sort_index()
