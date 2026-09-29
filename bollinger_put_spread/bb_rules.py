"""
Pure strategy rules for the Bollinger oversold put credit spread, kept free of any
Alpaca / network code so they can be tested offline (test_bot_offline.py) and reused
by a backtest later.

Daily bars are dicts with keys d (date), o, h, l, c, v -- oldest first.
"""
from __future__ import annotations

import csv
import io
import math
from datetime import date, timedelta

BB_PERIOD = 50           # 50-day SMA
BB_STD = 2.0             # 2 standard deviations
POC_LOOKBACK = 126       # ~6 months of daily bars
POC_BINS = 100           # price bins across the 6-month high-low range
WIDTH_PCT = 0.01         # target spread width = 1% of the underlying price
TAKE_PROFIT_FRAC = 0.50  # close when spread value <= 50% of entry credit
STOP_MULT = 2.0          # close when spread value >= 2x entry credit
TIME_STOP_DTE = 21       # close at 21 DTE


# --------------------------------------------------------------------------- #
# entry signal
# --------------------------------------------------------------------------- #
def bollinger(closes: list[float], i: int, n: int = BB_PERIOD, k: float = BB_STD):
    """(sma, lower, upper) for bar i, or None if there aren't n closes yet.
    Population standard deviation, the usual Bollinger convention."""
    if i < n - 1:
        return None
    w = closes[i - n + 1:i + 1]
    m = sum(w) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in w) / n)
    return m, m - k * sd, m + k * sd


def cross_below_lower(bars: list[dict], i: int | None = None) -> dict | None:
    """Signal on bar i (default: the last bar): close[i] < lower[i] and
    close[i-1] >= lower[i-1]. Returns the band values if it fired, else None."""
    if i is None:
        i = len(bars) - 1
    closes = [b["c"] for b in bars]
    today, yday = bollinger(closes, i), bollinger(closes, i - 1)
    if today is None or yday is None:
        return None
    if closes[i] < today[1] and closes[i - 1] >= yday[1]:
        return {"sma": today[0], "lower": today[1], "upper": today[2],
                "close": closes[i], "prev_close": closes[i - 1], "prev_lower": yday[1]}
    return None


def cross_above_lower(bars: list[dict], i: int | None = None) -> dict | None:
    """Reclaim signal (backtest variant A): close[i] >= lower[i] after close[i-1] < lower[i-1],
    i.e. the stock closes back inside the bands after an oversold close."""
    if i is None:
        i = len(bars) - 1
    closes = [b["c"] for b in bars]
    today, yday = bollinger(closes, i), bollinger(closes, i - 1)
    if today is None or yday is None:
        return None
    if closes[i] >= today[1] and closes[i - 1] < yday[1]:
        return {"sma": today[0], "lower": today[1], "upper": today[2],
                "close": closes[i], "prev_close": closes[i - 1], "prev_lower": yday[1]}
    return None


# --------------------------------------------------------------------------- #
# strike selection
# --------------------------------------------------------------------------- #
def volume_profile_poc(bars: list[dict], lookback: int = POC_LOOKBACK, nbins: int = POC_BINS) -> float:
    """Point of control of the last `lookback` bars: each bar's volume is spread evenly
    across its high-low range and dropped into `nbins` equal price bins; the POC is the
    midpoint of the bin holding the most volume."""
    w = bars[-lookback:]
    lo = min(b["l"] for b in w)
    hi = max(b["h"] for b in w)
    if hi <= lo:
        return (hi + lo) / 2
    size = (hi - lo) / nbins
    vol = [0.0] * nbins
    for b in w:
        bl, bh, v = b["l"], b["h"], float(b["v"])
        if v <= 0:
            continue
        if bh <= bl:                                   # zero-range bar: all volume in one bin
            vol[min(int((bl - lo) / size), nbins - 1)] += v
            continue
        first = min(int((bl - lo) / size), nbins - 1)
        last = min(int((bh - lo) / size), nbins - 1)
        for j in range(first, last + 1):
            b0, b1 = lo + j * size, lo + (j + 1) * size
            overlap = min(bh, b1) - max(bl, b0)
            if overlap > 0:
                vol[j] += v * overlap / (bh - bl)
    j = max(range(nbins), key=lambda k: vol[k])
    return lo + (j + 0.5) * size


def target_width(price: float, pct: float = WIDTH_PCT) -> float:
    """Spread width as a share of the underlying price: 1% -> $6.08 on a $607.87 stock."""
    return price * pct


def pick_strikes(listed: list[float], poc: float, lower_band: float,
                 width: float) -> tuple[float, float] | None:
    """Short put = highest listed strike strictly below BOTH the POC and the lower band.
    Long put = the listed strike below the short that is closest to (short - width); on a
    tie the narrower spread wins. So when strikes are spaced wider than `width`, the long
    is simply the next strike down. None if there is no short or no strike below it."""
    ceiling = min(poc, lower_band)
    below = [k for k in listed if k < ceiling - 1e-9]
    if not below:
        return None
    short = max(below)
    lower = [k for k in listed if k < short - 1e-9]
    if not lower:
        return None
    goal = short - width
    long = min(lower, key=lambda k: (abs(k - goal), short - k))
    return short, long


# --------------------------------------------------------------------------- #
# expiration
# --------------------------------------------------------------------------- #
def third_friday(year: int, month: int) -> date:
    d = date(year, month, 15)                     # the 3rd Friday falls on the 15th-21st
    return d + timedelta(days=(4 - d.weekday()) % 7)


def is_standard_monthly(exp: date, listed: set[date] | None = None) -> bool:
    """3rd Friday of the month -- or the Thursday before it when that Friday is a market
    holiday (e.g. Good Friday), which shows up as the Thursday being listed and the
    Friday not."""
    tf = third_friday(exp.year, exp.month)
    if exp == tf:
        return True
    return listed is not None and exp == tf - timedelta(days=1) and tf not in listed


def eligible_expirations(listed: list[date], today: date, next_earnings: date | None,
                         dte_min: int = 45, dte_max: int = 90) -> list[date]:
    """Standard monthlies with dte_min..dte_max DTE that expire BEFORE the next earnings
    date (an expiration on the earnings day itself is skipped too). Sorted, nearest first."""
    s = set(listed)
    out = []
    for e in sorted(s):
        if not (dte_min <= (e - today).days <= dte_max):
            continue
        if not is_standard_monthly(e, s):
            continue
        if next_earnings is not None and e >= next_earnings:
            continue
        out.append(e)
    return out


# --------------------------------------------------------------------------- #
# exits
# --------------------------------------------------------------------------- #
def exit_reason(entry_credit: float, spread_value: float | None, underlying: float,
                short_strike: float, dte: int) -> str | None:
    """Which exit (if any) fires today, checked in this order: stop-loss, backup stop,
    take profit, time stop. spread_value = short mid - long mid (the cost to close)."""
    if spread_value is not None and spread_value >= STOP_MULT * entry_credit:
        return "stop_loss"
    if underlying < short_strike:
        return "backup_stop"
    if spread_value is not None and spread_value <= TAKE_PROFIT_FRAC * entry_credit:
        return "take_profit"
    if dte <= TIME_STOP_DTE:
        return "time_stop"
    return None


# --------------------------------------------------------------------------- #
# portfolio limits
# --------------------------------------------------------------------------- #
def limit_violation(ticker: str, sector: str, book: list[dict],
                    max_total: int, max_sector: int, max_ticker: int) -> str | None:
    """book = spreads currently open or being opened, each with 'ticker' and 'sector'."""
    if len(book) >= max_total:
        return f"max {max_total} open spreads reached"
    if sum(1 for p in book if p["sector"] == sector) >= max_sector:
        return f"max {max_sector} spreads in sector {sector} reached"
    if sum(1 for p in book if p["ticker"] == ticker) >= max_ticker:
        return f"already {max_ticker} spread open on {ticker}"
    return None


def parse_earnings_csv(text: str, ticker: str, today: date) -> tuple[bool, date | None]:
    """Parse Alpha Vantage EARNINGS_CALENDAR CSV. Returns (ok, next_report_date_on_or_after_today).
    ok is False when the response isn't the expected CSV (rate limit note, error JSON...)."""
    rows = list(csv.reader(io.StringIO(text.strip())))
    if not rows or not rows[0] or rows[0][0].strip().lower() != "symbol":
        return False, None
    head = [h.strip() for h in rows[0]]
    try:
        si, di = head.index("symbol"), head.index("reportDate")
    except ValueError:
        return False, None
    dates = []
    for cols in rows[1:]:
        if len(cols) <= max(si, di) or cols[si].strip().upper() != ticker.upper():
            continue
        try:
            d = date.fromisoformat(cols[di].strip())
        except ValueError:
            continue
        if d >= today:
            dates.append(d)
    return True, (min(dates) if dates else None)
