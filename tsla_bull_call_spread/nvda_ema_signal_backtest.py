import json, math
from datetime import date, timedelta

# ============================================================================
# Real historical backtest of the entry signal: 3 consecutive daily closes
# above the 8-day EMA. Trade construction/management = the settled final
# strategy (nvda_final_strategy_audit.py): 30-delta long / 10-wide short,
# close-all at 45%, no stop, close by expiration. Earnings-avoidance: no new
# entries during the calendar week of a known/expected earnings date.
#
# DATA CONSTRAINT (disclosed): TIME_SERIES_DAILY outputsize=full is premium-
# gated on this Alpha Vantage key; only the compact ~100-day window is real
# data here (2026-04-28..2026-09-18). This is NOT enough history for a
# statistically meaningful backtest of a signal this selective -- treat the
# trade count/results below as a real-data smoke test of the mechanics, not
# a performance claim. A longer window needs either a premium key or another
# data source (Yahoo Finance was tried and is blocked by network policy in
# this environment).
# ============================================================================

with open("data/nvda_daily_2026-09-18.json") as f:
    raw_ts = json.load(f)["Time Series (Daily)"]
dts = sorted(raw_ts.keys())
closes = [float(raw_ts[d]["4. close"]) for d in dts]
dates = [date.fromisoformat(d) for d in dts]
N = len(closes)

# ---------- Real known/expected NVDA earnings dates (Alpha Vantage EARNINGS, fetched 2026-09-19) ----------
# Past (reported, real): 2026-08-26 falls inside our price-data window.
# Future (expected, user-provided, consistent with the historical Nov 14-21 pattern
# visible in the real EARNINGS data -- 2025-11-19, 2024-11-20, 2023-11-21, 2022-11-16...):
EARNINGS_DATES = [date(2026, 8, 26), date(2026, 11, 18)]

def in_earnings_week(d):
    for ed in EARNINGS_DATES:
        week_start = ed - timedelta(days=ed.weekday())       # Monday
        week_end = week_start + timedelta(days=4)             # Friday
        if week_start <= d <= week_end:
            return True, ed
    return False, None

# ---------- 8-day EMA on real closes ----------
EMA_PERIOD = 8
alpha = 2 / (EMA_PERIOD + 1)
ema = [None] * N
seed = sum(closes[:EMA_PERIOD]) / EMA_PERIOD
ema[EMA_PERIOD - 1] = seed
for i in range(EMA_PERIOD, N):
    ema[i] = closes[i] * alpha + ema[i - 1] * (1 - alpha)

# ---------- Black-Scholes (same math as the final strategy audit) ----------
def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def bs_call(S, K, T, r, sigma):
    if T <= 0:
        return max(0.0, S - K)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)

def bs_call_delta(S, K, T, r, sigma):
    if T <= 0:
        return 1.0 if S > K else 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    return norm_cdf(d1)

def find_30delta_strike(S, T, r, sigma, target=0.30):
    lo, hi = S, S * 1.6
    for _ in range(60):
        mid = (lo + hi) / 2
        if bs_call_delta(S, mid, T, r, sigma) > target:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2)

R_FREE = 0.04
WIDTH = 10.0
FEE_PER_CONTRACT = 0.65
SLIPPAGE_PCT = 0.04
DTE = 30
PROFIT_TARGET = 0.45
SIGMA = 0.391  # trimmed realized vol from the final strategy audit -- static for the whole
               # backtest (disclosed simplification: real IV would move day to day, especially
               # collapsing right after the 2026-08-26 print, but no historical IV is available)

def spread_value(S, K_long, K_short, days_left, sigma):
    T = max(days_left, 0) / 365.0
    if T <= 0:
        return max(0.0, min(S, K_short) - K_long)
    return bs_call(S, K_long, T, R_FREE, sigma) - bs_call(S, K_short, T, R_FREE, sigma)

# ---------- Walk the real price series, find signals, simulate trades ----------
print(f"Backtest window: {dates[0]} .. {dates[-1]}  ({N} real trading days)")
print(f"Entry signal: close > 8-EMA for 3 consecutive days, no open position, not earnings week")
print(f"Management: 30-delta/10-wide, close-all @45%, no stop, close by expiration (static "
      f"sigma={SIGMA:.1%})\n")

trades = []
in_position = False
position_close_idx = None
i = EMA_PERIOD + 2  # need 3 valid EMA-comparison days
while i < N:
    if in_position and i < position_close_idx:
        i += 1
        continue
    in_position = False

    signal = (closes[i] > ema[i] and closes[i-1] > ema[i-1] and closes[i-2] > ema[i-2])
    if signal:
        entry_idx = i
        entry_date = dates[entry_idx]
        blocked, which_earn = in_earnings_week(entry_date)
        if blocked:
            print(f"  [skip] {entry_date}: signal fired but blocked -- inside earnings week of {which_earn}")
            i += 1
            continue

        S0 = closes[entry_idx]
        K_long = find_30delta_strike(S0, DTE / 365.0, R_FREE, SIGMA)
        K_short = K_long + WIDTH
        debit = bs_call(S0, K_long, DTE/365.0, R_FREE, SIGMA) - bs_call(S0, K_short, DTE/365.0, R_FREE, SIGMA)
        debit = debit * (1 + SLIPPAGE_PCT) + FEE_PER_CONTRACT / 100.0

        exit_idx, exit_reason, ret = None, None, None
        last_avail = min(entry_idx + DTE, N - 1)
        for day in range(entry_idx + 1, last_avail + 1):
            days_held = day - entry_idx
            val = spread_value(closes[day], K_long, K_short, DTE - days_held, SIGMA)
            r = val / debit - 1.0
            if r >= PROFIT_TARGET or day == last_avail:
                exit_idx, ret = day, max(r, -1.0)
                exit_reason = "profit_target" if r >= PROFIT_TARGET else (
                    "expiration" if days_held >= DTE else "data_cutoff (still open)")
                break

        trades.append(dict(entry_date=entry_date, exit_date=dates[exit_idx], S0=S0,
                            S_exit=closes[exit_idx], K_long=K_long, K_short=K_short,
                            debit=debit, ret=ret, reason=exit_reason,
                            days_held=exit_idx - entry_idx))
        in_position = True
        position_close_idx = exit_idx
        i = exit_idx + 1
        continue
    i += 1

print(f"=== {len(trades)} trade(s) found in the real {N}-day window ===\n")
for t in trades:
    print(f"  {t['entry_date']} -> {t['exit_date']}  ({t['days_held']}d)  "
          f"S0=${t['S0']:.2f} -> exit S=${t['S_exit']:.2f}  "
          f"{t['K_long']:.0f}C/{t['K_short']:.0f}C debit=${t['debit']:.2f}  "
          f"ret={t['ret']:+.1%}  [{t['reason']}]")

if trades:
    resolved = [t for t in trades if t['reason'] != 'data_cutoff (still open)']
    open_trades = [t for t in trades if t['reason'] == 'data_cutoff (still open)']
    if resolved:
        rets = [t['ret'] for t in resolved]
        win_rate = sum(1 for r in rets if r > 0) / len(rets)
        mean_ret = sum(rets) / len(rets)
        print(f"\nResolved trades: n={len(resolved)}  win_rate={win_rate:.0%}  mean_ret={mean_ret:+.1%}")
    if open_trades:
        print(f"Still open at data cutoff (2026-09-18), excluded from the stats above: "
              f"{len(open_trades)} trade(s) -- its return shown is a mark, not a resolved outcome.")
    print("\nSample size is too small (n={}) to draw a performance conclusion -- this is a "
          "real-data mechanics check, not a backtest result you should size a strategy on. "
          "See the note at the top of this file for why the window is this short.".format(len(resolved)))
else:
    print("No qualifying entry signals in this window (or all were blocked by earnings week).")
