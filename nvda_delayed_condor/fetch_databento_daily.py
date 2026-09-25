"""
Download NVDA daily price bars from Databento and save them as a CSV the backtest can use.

Needs a Databento API KEY (not your login/password): databento.com -> sign in ->
API Keys. Put it in the .env file next to this script:
    DATABENTO_API_KEY=db-xxxxxxxxxxxxxxxxxxxxxxxxxxxx

Run:  python fetch_databento_daily.py
It prints Databento's cost estimate first and only downloads after you type y.

Output: nvda_daily_databento.csv  (date, open, high, low, close, volume), split-adjusted
to today's share count so it lines up with the backtest's weekly data.

Source: dataset XNAS.ITCH (Nasdaq exchange feed, from 2018-05-01), schema ohlcv-1d.
These are Nasdaq-exchange bars: highs/lows can differ by cents from the consolidated
tape and volume is Nasdaq-only, which doesn't matter for this strategy.
"""
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

import databento as db

HERE = Path(__file__).resolve().parent
OUT = HERE / "nvda_daily_databento.csv"
DATASET = "XNAS.ITCH"
SYMBOL = "NVDA"
START = "2018-05-01"

# NVDA splits after START: (effective date, ratio). Prices before a split are divided by
# the ratio (volume multiplied) so the whole series is in today's share terms.
SPLITS = [(date(2021, 7, 20), 4), (date(2024, 6, 10), 10)]


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def split_adjust(df):
    df = df.copy()
    for eff, ratio in SPLITS:
        before = df["date"] < eff
        for col in ("open", "high", "low", "close"):
            df.loc[before, col] = df.loc[before, col] / ratio
        df.loc[before, "volume"] = df.loc[before, "volume"] * ratio
    return df


def to_daily_table(raw):
    """Databento ohlcv-1d frame (indexed by ts_event) -> date/open/high/low/close/volume."""
    out = raw.reset_index()[["ts_event", "open", "high", "low", "close", "volume"]].copy()
    out["date"] = out["ts_event"].dt.date
    out = out.drop(columns="ts_event")[["date", "open", "high", "low", "close", "volume"]]
    return out.sort_values("date").drop_duplicates("date", keep="last")


def main():
    load_env_file(HERE / ".env")
    key = os.environ.get("DATABENTO_API_KEY")
    if not key:
        print("Missing DATABENTO_API_KEY (add it to the .env file next to this script).")
        sys.exit(1)
    client = db.Historical(key)

    end = client.metadata.get_dataset_range(dataset=DATASET)["end"]
    cost = client.metadata.get_cost(dataset=DATASET, symbols=[SYMBOL], schema="ohlcv-1d",
                                    start=START, end=end)
    print(f"{DATASET} {SYMBOL} ohlcv-1d {START} .. {end}: estimated cost ${cost:,.2f}")
    if input("Download? [y/N] ").strip().lower() != "y":
        print("Cancelled, nothing downloaded.")
        return

    data = client.timeseries.get_range(dataset=DATASET, symbols=[SYMBOL], schema="ohlcv-1d",
                                       start=START, end=end)
    df = split_adjust(to_daily_table(data.to_df()))
    df.to_csv(OUT, index=False, float_format="%.4f")
    print(f"Saved {len(df)} days ({df['date'].iloc[0]} .. {df['date'].iloc[-1]}) to {OUT.name}")
    print(df.tail(3).to_string(index=False))


if __name__ == "__main__":
    main()
