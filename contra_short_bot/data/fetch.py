"""Download Databento 1-minute bars and cache them as one parquet file per symbol.

    python -m data.fetch                 # favorites + index proxies, all datasets
    python -m data.fetch --symbols NVDA TSLA --dataset XNAS.ITCH

Bars are stored with an America/New_York timestamp index (bar open time) and
columns open, high, low, close, volume. Re-running only fetches days after the
last cached bar.
"""
from __future__ import annotations

import argparse
import os
from functools import lru_cache
from pathlib import Path

import databento as db
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
ET = "America/New_York"


@lru_cache(maxsize=1)
def load_config() -> dict:
    return yaml.safe_load((ROOT / "config.yaml").read_text())


def default_symbols(cfg: dict) -> list[str]:
    fav = yaml.safe_load((ROOT / "watchlist.yaml").read_text())["favorites"]
    return list(dict.fromkeys(fav + list(cfg["data"]["index_proxies"]) + cfg["data"]["extra_symbols"]))


def cache_path(cfg: dict, dataset: str, symbol: str) -> Path:
    return ROOT / cfg["data"]["cache_dir"] / dataset / f"{symbol}.parquet"


def _client() -> db.Historical:
    # The cloud session's proxy injects credentials; on the laptop set DATABENTO_API_KEY.
    return db.Historical(os.environ.get("DATABENTO_API_KEY", "db-" + "x" * 29))


def _segments(cfg: dict, symbol: str, start: str, end: str) -> list[tuple[str, str, str]]:
    """Split [start, end) into (raw_symbol, start, end) pieces using the ticker history."""
    segs, cur = [], start
    for h in cfg["data"].get("symbol_history", {}).get(symbol, []):
        if cur < h["until"]:
            segs.append((h["symbol"], cur, min(h["until"], end)))
            cur = h["until"]
    if cur < end:
        segs.append((symbol, cur, end))
    return segs


def fetch(dataset: str, symbols: list[str], start: str, end: str, cfg: dict) -> None:
    client = _client()
    for sym in symbols:
        path = cache_path(cfg, dataset, sym)
        old = pd.read_parquet(path) if path.exists() else None
        s = start
        if old is not None and len(old):
            s = max(start, (old.index.max().tz_convert("UTC") + pd.Timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M"))
        frames = [] if old is None else [old]
        for raw, a, b in _segments(cfg, sym, s, end):
            if a >= b:
                continue
            data = client.timeseries.get_range(dataset=dataset, schema="ohlcv-1m", symbols=[raw], start=a, end=b)
            df = data.to_df()
            if df.empty:
                continue
            df = df[["open", "high", "low", "close", "volume"]].astype(
                {"open": float, "high": float, "low": float, "close": float, "volume": "int64"})
            df.index = df.index.tz_convert(ET)
            df.index.name = "ts"
            frames.append(df)
        if not frames:
            print(f"{dataset} {sym}: no data")
            continue
        out = pd.concat(frames)
        out = out[~out.index.duplicated(keep="last")].sort_index()
        path.parent.mkdir(parents=True, exist_ok=True)
        out.to_parquet(path)
        print(f"{dataset} {sym}: {len(out):,} bars {out.index.min():%Y-%m-%d} .. {out.index.max():%Y-%m-%d}")


def fetch_summary(symbols: list[str], end: str, cfg: dict) -> None:
    """Official consolidated daily bars (EQUS.SUMMARY, from 2024-07). Used only to
    measure each stock's Nasdaq share of total volume, so share-count thresholds
    written for the full tape can be scaled to the Nasdaq-only intraday data."""
    d = cfg["data"]
    path = ROOT / d["cache_dir"] / d["summary_dataset"] / "daily.parquet"
    data = _client().timeseries.get_range(dataset=d["summary_dataset"], schema="ohlcv-1d", symbols=symbols,
                                          start=d["summary_start"], end=end)
    df = data.to_df()[["symbol", "open", "high", "low", "close", "volume"]]
    df.index = df.index.tz_convert("UTC").tz_localize(None).normalize()
    df.index.name = "date"
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path)
    print(f"{d['summary_dataset']}: {len(df):,} daily bars for {df['symbol'].nunique()} symbols")


def main() -> None:
    cfg = load_config()
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="*")
    ap.add_argument("--dataset", choices=["all", cfg["data"]["intraday_dataset"], cfg["data"]["summary_dataset"]],
                    default="all")
    ap.add_argument("--end", default=pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d"))
    args = ap.parse_args()
    symbols = args.symbols or default_symbols(cfg)
    d = cfg["data"]
    if args.dataset in ("all", d["intraday_dataset"]):
        fetch(d["intraday_dataset"], symbols, d["intraday_start"], args.end, cfg)
    if args.dataset in ("all", d["summary_dataset"]):
        fetch_summary(symbols, args.end, cfg)


if __name__ == "__main__":
    main()
