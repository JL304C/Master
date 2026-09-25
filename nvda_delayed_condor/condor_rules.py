"""
Signal rules for the NVDA delayed iron condor, shared by the backtest and the bot so
both trade exactly the same definition.

All functions take a list of WEEKLY bars (oldest first), each a dict with keys
o, h, l, c (split-adjusted), and an index i (the bar being evaluated).
"""
from datetime import date, timedelta

# ---- entry: "declining but finding technical support" ----
PULLBACK = 0.07        # last 2 weeks' low >= 7% below the 5-week closing high
NEAR_SUPPORT = 0.06    # close no more than 6% above the support level
TEST_BAND = 0.03       # the pullback low came within +/-3% of support
MIN_BARS = 45

# ---- call add: "risen / near resistance / overbought" ----
RISEN_PCT = 0.02       # >= 2% above the underlying price at put entry
RESIST_PCT = 0.03      # within 3% of the 10-week high
RSI_OB = 65            # weekly RSI(14)


def sma(bars, i, n):
    if i < n - 1:
        return None
    return sum(b['c'] for b in bars[i - n + 1:i + 1]) / n


def rsi(bars, i, n=14):
    if i < n:
        return None
    g = l = 0.0
    for k in range(i - n + 1, i + 1):
        ch = bars[k]['c'] - bars[k - 1]['c']
        if ch > 0:
            g += ch
        else:
            l -= ch
    if l == 0:
        return 100.0
    return 100 - 100 / (1 + g / l)


def entry_signal(bars, i, pullback=PULLBACK, near=NEAR_SUPPORT):
    """Returns the support price if bar i is 'declining but finding support', else None.

       declining : the last 2 weeks' low is >= pullback below the 5-week closing high
       support   : nearest of 10w SMA (~50d), 20w SMA (~100d), 40w SMA (~200d), or the
                   prior swing low (lowest low of weeks i-12..i-3) at/below the close;
                   the close must be within `near` above it
       held      : the last 2 weeks' low tested support (within +/-3%) and held, AND the
                   week closed up or in the upper half of its range
    """
    if i < MIN_BARS:
        return None
    c = bars[i]['c']
    w = bars[i]
    hi = max(b['c'] for b in bars[i - 5:i + 1])
    lo2 = min(bars[i]['l'], bars[i - 1]['l'])
    if lo2 > hi * (1 - pullback):
        return None
    levels = [sma(bars, i, 10), sma(bars, i, 20), sma(bars, i, 40),
              min(b['l'] for b in bars[i - 12:i - 2])]
    below = [lv for lv in levels if lv and lv <= c]
    if not below:
        return None
    sup = max(below)
    if (c - sup) / sup > near:
        return None
    if not (sup * (1 - TEST_BAND) <= lo2 <= sup * (1 + TEST_BAND)):
        return None
    rng = w['h'] - w['l']
    stalling = c >= bars[i - 1]['c'] or (rng > 0 and (c - w['l']) / rng >= 0.5)
    if not stalling:
        return None
    return sup


def call_add_signal(bars, i, entry_price):
    """True if NVDA has risen since the put entry, is near resistance, or is overbought.
    entry_price may be None (unknown) -- then only the other two tests apply."""
    c = bars[i]['c']
    risen = entry_price is not None and c > entry_price * (1 + RISEN_PCT)
    near_res = c >= (1 - RESIST_PCT) * max(b['h'] for b in bars[max(0, i - 10):i + 1])
    ob = (rsi(bars, i) or 0) >= RSI_OB
    return risen or near_res or ob


def earnings_between(earnings, d0, d1):
    """Any earnings report after d0 and on/before d1."""
    return any(d0 < e <= d1 for e in earnings)


def load_earnings(path):
    out = []
    with open(path) as f:
        for line in f:
            line = line.split('#', 1)[0].strip()
            if line:
                out.append(date.fromisoformat(line))
    return sorted(out)
