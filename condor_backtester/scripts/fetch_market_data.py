"""Download free daily market data and write data/market.csv.

Sources (no API key needed):
    SPX open/close   https://cdn.cboe.com/api/global/us_indices/daily_prices/SPX_History.csv
    VIX close        https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv
    VIX3M close      https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX3M_History.csv
    3m T-bill yield  https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS3MO

If CBOE blocks the download, it falls back to Yahoo Finance via the
``yfinance`` package (pip install yfinance): ^GSPC, ^VIX, ^VIX3M, ^IRX.

Usage:
    python scripts/fetch_market_data.py                  # writes data/market.csv
    python scripts/fetch_market_data.py --start 2015-01-01 --out data/market.csv
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import urllib.request

import pandas as pd

CBOE = "https://cdn.cboe.com/api/global/us_indices/daily_prices/{}_History.csv"
FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS3MO"
UA = {"User-Agent": "Mozilla/5.0 (condor-backtester data fetch)"}


def _get(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", errors="replace")


def _cboe(symbol: str) -> pd.DataFrame:
    df = pd.read_csv(io.StringIO(_get(CBOE.format(symbol))))
    df.columns = [c.strip().lower() for c in df.columns]
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


def _fred_rate() -> pd.DataFrame:
    df = pd.read_csv(io.StringIO(_get(FRED)), na_values=".")
    df.columns = [c.strip().lower() for c in df.columns]
    date_col = "observation_date" if "observation_date" in df else "date"
    return pd.DataFrame({"date": pd.to_datetime(df[date_col]).dt.date,
                         "rate": pd.to_numeric(df["dgs3mo"], errors="coerce") / 100.0})


def from_cboe() -> pd.DataFrame:
    spx = _cboe("SPX")[["date", "open", "close"]]
    vix = _cboe("VIX")[["date", "close"]].rename(columns={"close": "vix"})
    try:
        vix3m = _cboe("VIX3M")[["date", "close"]].rename(columns={"close": "vix3m"})
    except Exception as exc:
        print(f"VIX3M download failed ({exc}); continuing without term structure", file=sys.stderr)
        vix3m = pd.DataFrame(columns=["date", "vix3m"])
    df = spx.merge(vix, on="date", how="left").merge(vix3m, on="date", how="left")
    try:
        df = df.merge(_fred_rate(), on="date", how="left")
    except Exception as exc:
        print(f"FRED rate download failed ({exc}); the config's risk_free_rate will be used", file=sys.stderr)
    return df


def from_yahoo(start: str) -> pd.DataFrame:
    import yfinance as yf  # optional dependency

    def hist(t):
        h = yf.Ticker(t).history(start=start, auto_adjust=False)
        h.index = h.index.tz_localize(None).date
        return h

    spx, vix, vix3m, irx = hist("^GSPC"), hist("^VIX"), hist("^VIX3M"), hist("^IRX")
    df = pd.DataFrame({"open": spx["Open"], "close": spx["Close"]})
    df["vix"] = vix["Close"]
    df["vix3m"] = vix3m["Close"]
    df["rate"] = irx["Close"] / 100.0
    return df.rename_axis("date").reset_index()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2010-01-01")
    ap.add_argument("--out", default=os.path.join("data", "market.csv"))
    ap.add_argument("--source", choices=["auto", "cboe", "yahoo"], default="auto")
    a = ap.parse_args()
    df = None
    if a.source in ("auto", "cboe"):
        try:
            df = from_cboe()
        except Exception as exc:
            if a.source == "cboe":
                raise
            print(f"CBOE download failed ({exc}); trying Yahoo Finance", file=sys.stderr)
    if df is None:
        df = from_yahoo(a.start)
    df = df[pd.to_datetime(df["date"]) >= pd.Timestamp(a.start)].sort_values("date")
    # CBOE's SPX history sometimes has open == 0 or open == close on old rows; drop bad opens
    df.loc[df["open"] <= 0, "open"] = float("nan")
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    df.to_csv(a.out, index=False)
    print(f"wrote {len(df)} rows {df['date'].iloc[0]} -> {df['date'].iloc[-1]} to {a.out}")
    print(df.tail(3).to_string(index=False))


if __name__ == "__main__":
    main()
