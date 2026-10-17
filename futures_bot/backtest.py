"""Backtest three simple strategies on MNQ and MGC micro futures, 1-hour bars.

Goal: pick a strategy for a futures paper bot (moomoo, a few trades a day)
before writing the bot. One contract at a time, long or short, with costs.

Data: yfinance continuous front-month (MNQ=F, MGC=F), ~2.4 years of 60m bars,
cached to data/. Continuous contracts are NOT back-adjusted, so each roll adds
a price jump; trades that span a roll get a small false P&L. Treat results as
a ranking of ideas, not exact numbers.

Each strategy is reported on the first 70% of the data and the last 30%
separately. Nothing is tuned, so the later 30% is an honest out-of-sample check.

Run: python backtest.py            (uses cached data if present)
     python backtest.py --refresh  (re-download)
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).parent
DATA = HERE / "data"

# point value ($ per 1.0 price move), tick size
SPECS = {
    "MNQ": {"yf": "MNQ=F", "point": 2.0, "tick": 0.25},
    "MGC": {"yf": "MGC=F", "point": 10.0, "tick": 0.10},
}
COMMISSION_PER_SIDE = 1.25  # approx moomoo micro futures commission + exchange fees
SLIPPAGE_TICKS = 1  # per side


def load(sym: str, refresh: bool) -> pd.DataFrame:
    DATA.mkdir(exist_ok=True)
    path = DATA / f"{sym}_60m.csv"
    if path.exists() and not refresh:
        d = pd.read_csv(path, index_col=0)
        # mixed -04:00/-05:00 offsets across DST: parse as UTC, show in ET
        d.index = pd.to_datetime(d.index, utc=True).tz_convert("America/New_York")
        return d
    import yfinance as yf

    d = yf.download(SPECS[sym]["yf"], interval="60m", period="730d", progress=False, auto_adjust=False)
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = d.columns.get_level_values(0)
    d = d[["Open", "High", "Low", "Close"]].dropna()
    d.to_csv(path)
    return d


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df.Close.shift()
    tr = pd.concat([df.High - df.Low, (df.High - pc).abs(), (df.Low - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


# --- strategies: each returns a target position per bar (-1, 0, +1), decided
# on the bar's close and filled at the next bar's open ---


def trend_ema(df: pd.DataFrame) -> pd.Series:
    """EMA 20/50 cross: long when fast > slow, short when below. Always in."""
    return np.sign(ema(df.Close, 20) - ema(df.Close, 50)).fillna(0)


def mean_reversion_rsi(df: pd.DataFrame) -> pd.Series:
    """RSI(14) < 30 -> long, > 70 -> short; exit when RSI crosses 50 or a
    2 x ATR stop from entry is hit."""
    r, a = rsi(df.Close), atr(df)
    pos, entry, out = 0, 0.0, []
    for c, ri, ai in zip(df.Close.values, r.values, a.values):
        if pos == 1 and (ri >= 50 or c <= entry - 2 * ai):
            pos = 0
        elif pos == -1 and (ri <= 50 or c >= entry + 2 * ai):
            pos = 0
        if pos == 0:
            if ri < 30:
                pos, entry = 1, c
            elif ri > 70:
                pos, entry = -1, c
        out.append(pos)
    return pd.Series(out, index=df.index, dtype=float)


def breakout_donchian(df: pd.DataFrame) -> pd.Series:
    """Close above the prior 20-bar high -> long; below the 20-bar low -> short;
    3 x ATR trailing stop."""
    hi, lo, a = df.High.rolling(20).max().shift(), df.Low.rolling(20).min().shift(), atr(df)
    pos, stop, out = 0, 0.0, []
    for c, h, l, ai in zip(df.Close.values, hi.values, lo.values, a.values):
        if pos == 1:
            stop = max(stop, c - 3 * ai)
            if c <= stop:
                pos = 0
        elif pos == -1:
            stop = min(stop, c + 3 * ai)
            if c >= stop:
                pos = 0
        if pos == 0 and not np.isnan(h):
            if c > h:
                pos, stop = 1, c - 3 * ai
            elif c < l:
                pos, stop = -1, c + 3 * ai
        out.append(pos)
    return pd.Series(out, index=df.index, dtype=float)


def et(df: pd.DataFrame) -> pd.DatetimeIndex:
    return pd.to_datetime(df.index, utc=True).tz_convert("America/New_York")


def daily_trend_up(df: pd.DataFrame, n: int = 50) -> pd.Series:
    """True on bars where the PREVIOUS day's close was above its n-day SMA,
    so the filter only uses information known before the day starts."""
    idx = et(df)
    day = pd.Series(idx.date, index=df.index)
    closes = df.Close.groupby(day.values).last()
    up = (closes > closes.rolling(n).mean()).shift()  # yesterday's verdict
    return day.map(up).fillna(False).astype(bool)


def range_break(df: pd.DataFrame, range_hours: list[int], trade_hours: list[int],
                exit_hour: int, allow: pd.Series | None = None) -> pd.Series:
    """One trade a day: build a high/low range from `range_hours` bars (ET),
    then during `trade_hours` go long on a close above it or short below it.
    Stop = the opposite side of the range. Flat at the close of `exit_hour`.
    `allow`, if given, restricts entries to long-only on days it is True and
    no trades on days it is False."""
    idx = et(df)
    pos, out = 0, []
    rng_hi = rng_lo = None
    traded = False
    cur_day = None
    for i, (t, c, h, l) in enumerate(zip(idx, df.Close.values, df.High.values, df.Low.values)):
        sess = (t + pd.Timedelta(hours=6)).date()  # futures day starts 18:00 ET
        if sess != cur_day:
            cur_day, rng_hi, rng_lo, traded, pos = sess, None, None, False, 0
        if t.hour in range_hours:
            rng_hi = h if rng_hi is None else max(rng_hi, h)
            rng_lo = l if rng_lo is None else min(rng_lo, l)
        if pos == 1 and c <= rng_lo:
            pos = 0
        elif pos == -1 and c >= rng_hi:
            pos = 0
        if t.hour == exit_hour:
            pos = 0
        elif pos == 0 and not traded and rng_hi is not None and t.hour in trade_hours:
            ok = True if allow is None else bool(allow.iloc[i])
            if c > rng_hi and ok:
                pos, traded = 1, True
            elif c < rng_lo and ok and allow is None:
                pos, traded = -1, True
        out.append(pos)
    return pd.Series(out, index=df.index, dtype=float)


def orb_mnq(df: pd.DataFrame) -> pd.Series:
    """Opening-range breakout: range = 09:00 bar (covers the 09:30 open),
    entries 10:00-14:00 bars, flat at the 15:00 bar's close."""
    return range_break(df, [9], [10, 11, 12, 13, 14], exit_hour=15)


def orb_mnq_trend(df: pd.DataFrame) -> pd.Series:
    """Same ORB, long-only, and only on days the daily trend is up."""
    return range_break(df, [9], [10, 11, 12, 13, 14], exit_hour=15, allow=daily_trend_up(df))


def gold_london(df: pd.DataFrame) -> pd.Series:
    """Asian-range breakout: range = 18:00-02:00 ET bars, entries from the
    London open (03:00) to 09:00, flat at the 11:00 bar's close."""
    return range_break(df, [18, 19, 20, 21, 22, 23, 0, 1, 2], [3, 4, 5, 6, 7, 8, 9], exit_hour=11)


def trend_long_filtered(df: pd.DataFrame) -> pd.Series:
    """Long-only: long when 1h EMA 20 > EMA 50 AND the daily trend is up
    (previous close above the 50-day average); otherwise flat."""
    return ((ema(df.Close, 20) > ema(df.Close, 50)) & daily_trend_up(df)).astype(float)


# round 1: generic always-in strategies; round 2: session / long-biased ideas
ROUNDS = {
    1: {
        "Trend (EMA 20/50)": (trend_ema, ["MNQ", "MGC"]),
        "Mean reversion (RSI 14)": (mean_reversion_rsi, ["MNQ", "MGC"]),
        "Breakout (Donchian 20)": (breakout_donchian, ["MNQ", "MGC"]),
    },
    2: {
        "Opening-range breakout": (orb_mnq, ["MNQ"]),
        "ORB long-only + trend filter": (orb_mnq_trend, ["MNQ"]),
        "London Asian-range breakout": (gold_london, ["MGC"]),
        "Long-only trend + daily filter": (trend_long_filtered, ["MNQ", "MGC"]),
    },
}


def simulate(df: pd.DataFrame, target: pd.Series, spec: dict) -> tuple[pd.Series, int]:
    """Dollar P&L per bar for one contract. Position changes fill at the next
    bar's open; each contract traded pays commission + slippage."""
    pos = target.shift().fillna(0)  # decided at close, held from next bar
    prev_close = df.Close.shift()
    # bars where we were already holding the position from the prior bar
    held = pos.shift().fillna(0)
    pnl = pd.Series(np.where(pos == held, pos * (df.Close - prev_close), 0.0), index=df.index)
    # bars where the position changed: old position earns prev_close -> open,
    # new position earns open -> close
    chg = pos != held
    pnl[chg] = (held[chg] * (df.Open[chg] - prev_close[chg]) + pos[chg] * (df.Close[chg] - df.Open[chg]))
    pnl = pnl.fillna(0) * spec["point"]
    contracts = (pos - held).abs()
    cost = contracts * (COMMISSION_PER_SIDE + SLIPPAGE_TICKS * spec["tick"] * spec["point"])
    entries = int(((pos != 0) & (pos != held)).sum())
    return pnl - cost, entries


def stats(pnl: pd.Series, entries: int) -> dict:
    daily = pnl.groupby(pnl.index.date).sum()
    eq = pnl.cumsum()
    dd = (eq - eq.cummax()).min()
    gains, losses = daily[daily > 0].sum(), -daily[daily < 0].sum()
    days = max(len(daily), 1)
    return {
        "net $": round(pnl.sum()),
        "trades": entries,
        "trades/day": round(entries / days, 2),
        "max DD $": round(dd),
        "profit factor": round(gains / losses, 2) if losses else float("inf"),
        "Sharpe (daily)": round(daily.mean() / daily.std() * np.sqrt(252), 2) if daily.std() else 0.0,
        "win days %": round(100 * (daily > 0).mean()),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--round", type=int, default=2, choices=sorted(ROUNDS))
    args = ap.parse_args()

    rows = []
    for sym, spec in SPECS.items():
        df = load(sym, args.refresh)
        cut = df.index[int(len(df) * 0.7)]
        bh = (df.Close.iloc[-1] - df.Close.iloc[0]) * spec["point"]
        print(f"{sym}: {len(df)} bars {df.index[0]:%Y-%m-%d} -> {df.index[-1]:%Y-%m-%d}, "
              f"split at {cut:%Y-%m-%d}; buy & hold 1 contract = ${bh:,.0f}")
        for name, (fn, markets) in ROUNDS[args.round].items():
            if sym not in markets:
                continue
            target = fn(df)
            pnl, _ = simulate(df, target, spec)
            for label, mask in (("first 70%", df.index < cut), ("last 30%", df.index >= cut)):
                t = target[mask]
                held = t.shift().fillna(0)
                entries = int(((t != 0) & (t != held)).sum())
                rows.append({"market": sym, "strategy": name, "period": label, **stats(pnl[mask], entries)})
    out = pd.DataFrame(rows)
    pd.set_option("display.width", 200)
    print()
    print(out.to_string(index=False))
    out.to_csv(HERE / f"backtest_results_round{args.round}.csv", index=False)


if __name__ == "__main__":
    main()
