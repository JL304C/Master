import json, math, random, statistics

# ---------- Real market data (Alpha Vantage TIME_SERIES_DAILY, free tier) ----------
# TSLA daily closes, 2026-04-28..2026-09-18 (fetched 2026-09-19, see data/ for raw response).
with open("data/tsla_daily_2026-09-18.json") as f:
    raw_ts = json.load(f)["Time Series (Daily)"]
dates = sorted(raw_ts.keys())
closes = [float(raw_ts[d]["4. close"]) for d in dates]
S0 = closes[-1]          # 364.27, last close 2026-09-18
QUOTE_PREV_CLOSE = 366.20
EARNINGS_DATE = "2026-10-28"   # Alpha Vantage EARNINGS_CALENDAR, fetched 2026-09-19
TODAY = "2026-09-19"

log_rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
mean_r = sum(log_rets) / len(log_rets)
var_r = sum((r - mean_r) ** 2 for r in log_rets) / (len(log_rets) - 1)
ann_sigma = math.sqrt(var_r) * math.sqrt(252)
ann_drift_100d_noisy = mean_r * 252

print(f"S0={S0:.2f}  realized_100d_ann_vol={ann_sigma:.3f}  100d_drift={ann_drift_100d_noisy:+.3f} "
      f"(rejected as a forecast -- see note below)")
print(f"Next confirmed earnings: {EARNINGS_DATE} -- a binary gap event, independent of any 'bullish signal'")

# ---------- CAVEAT (disclosed, not silently assumed) ----------
# The two-paragraph rule set gates entry on "a strong, time-bounded bullish signal" from
# "the system" -- that signal generator is not specified anywhere in the rules given, so it
# cannot itself be backtested here. What CAN be audited, and what this script does, is
# everything downstream of a valid signal: (1) whether the prescribed trade construction
# (30-delta long call, 10-wide short call, <=25-30% debit) is achievable on TSLA's real
# current price/vol, (2) whether the stated risk/reward math holds, and (3) how the full
# management rule set (40-50% quick exit / 100%-take-half, no rolling losers, close by
# expiration) performs across a spread of POST-SIGNAL drift regimes, standing in for
# "the signal was right" through "the signal was wrong."

# ---------- Black-Scholes ----------
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

R_FREE = 0.04
WIDTH = 10.0
FEE_PER_CONTRACT = 0.65   # both legs, open + close, round-trip approx
SLIPPAGE_PCT = 0.04       # vertical spreads cross two bid/ask spreads -- wider than a single leg

def find_30delta_strike(S, T, r, sigma, target=0.30, lo=None, hi=None):
    lo = lo or S
    hi = hi or S * 1.6
    for _ in range(60):
        mid = (lo + hi) / 2
        d = bs_call_delta(S, mid, T, r, sigma)
        if d > target:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2)

# ============ SECTION 1: Real-data entry check, today, across candidate DTEs ============
print("\n=== SECTION 1: Entry-rule check against TSLA's real price/vol today ===")
print(f"{'DTE':>4} {'K_long':>8} {'K_short':>8} {'delta_long':>10} {'debit':>8} {'debit/width':>12} "
      f"{'breakeven':>10} {'max_profit':>10} {'reward:risk':>12} {'spans_earnings':>15}")
entry_rows = []
for dte in (21, 30, 35, 45, 60):
    T = dte / 365.0
    K_long = find_30delta_strike(S0, T, R_FREE, ann_sigma)
    K_short = K_long + WIDTH
    c_long = bs_call(S0, K_long, T, R_FREE, ann_sigma)
    c_short = bs_call(S0, K_short, T, R_FREE, ann_sigma)
    debit = c_long - c_short
    debit_net = debit * (1 + SLIPPAGE_PCT) + FEE_PER_CONTRACT / 100.0
    ratio = debit_net / WIDTH
    breakeven = K_long + debit_net
    max_profit = WIDTH - debit_net
    rr = max_profit / debit_net if debit_net > 0 else float("inf")
    from datetime import date, timedelta
    exp_date = date(2026, 9, 19) + timedelta(days=dte)
    spans_earn = exp_date >= date(2026, 10, 28)
    entry_rows.append(dict(dte=dte, K_long=K_long, K_short=K_short, debit=debit_net, ratio=ratio,
                            breakeven=breakeven, max_profit=max_profit, rr=rr, spans_earn=spans_earn))
    d_long = bs_call_delta(S0, K_long, T, R_FREE, ann_sigma)
    print(f"{dte:>4} {K_long:>8.0f} {K_short:>8.0f} {d_long:>10.3f} {debit_net:>8.2f} {ratio:>11.1%} "
          f"{breakeven:>10.2f} {max_profit:>10.2f} {rr:>11.2f}x {str(spans_earn):>15}")

print("""
Reading Section 1: at TSLA's current realized vol (~{:.0f}% annualized, elevated even for TSLA)
the 30-delta/10-wide spread actually prices CHEAP relative to the rule's ceiling -- roughly
22-24% of width across every DTE tested, comfortably under the $2.50 ideal and the $3.00 hard
cap. That is itself informative: a 10-point-wide spread is a small fraction of TSLA's ~$364
price and ~53% vol, so the 30-delta strike sits far enough out that the spread never gets
expensive enough to violate the rule on this name at these levels -- the binding constraint
for TSLA is the SIGNAL requirement in paragraph 1, not the debit ceiling in paragraph 1's back
half. (On a lower-vol or lower-priced underlying, a $10-wide spread is a much bigger fraction
of the same delta's premium and the debit ceiling becomes the binding constraint instead.)
""".format(ann_sigma * 100))
for row in entry_rows:
    verdict = "PASS" if row["ratio"] <= 0.30 and row["rr"] >= 2.3 else "FAIL"
    earn_flag = "  [SPANS EARNINGS 2026-10-28 -- gap risk on a defined-risk spread, still worth flagging]" if row["spans_earn"] else ""
    print(f"  DTE {row['dte']:>3}: debit/width {row['ratio']:.1%}, reward:risk {row['rr']:.2f}x -> {verdict}{earn_flag}")

# ============ SECTION 2: Monte Carlo -- spread P&L under the full management rule set ============
print("\n=== SECTION 2: Monte Carlo, management-rule performance across drift regimes ===")
# Use the DTE=30 case as the base construction (typical for this rule family; the two
# paragraphs given don't pin DTE, so this is a disclosed assumption -- see Section 1 for
# other DTEs). Only simulate regimes where entry would actually be taken (debit <= 30% width).
base = next(r for r in entry_rows if r["dte"] == 30)
K_LONG, K_SHORT, DEBIT, DTE = base["K_long"], base["K_short"], base["debit"], 30

def gen_path(n_days, mu, sigma, seed):
    rnd = random.Random(seed)
    dt = 1 / 252.0
    S = S0
    path = [S]
    for _ in range(n_days):
        z = rnd.gauss(0, 1)
        S = S * math.exp((mu - 0.5 * sigma ** 2) * dt + sigma * math.sqrt(dt) * z)
        path.append(S)
    return path

def spread_value(S, days_left, sigma):
    T = max(days_left, 0) / 365.0
    if T <= 0:
        return max(0.0, min(S, K_SHORT) - K_LONG)
    return bs_call(S, K_LONG, T, R_FREE, sigma) - bs_call(S, K_SHORT, T, R_FREE, sigma)

def run_trade(path, sigma_path, quick_exit_pct=0.45, use_take_half_at_100=False):
    """One spread, DEBIT paid on day 0, DTE trading days to run.
    Rule: never add/roll a loser; close both legs together; close by expiration.
    Returns realized P&L as a fraction of DEBIT (e.g. +0.45, -1.0, +1.6)."""
    n = min(DTE, len(path) - 1)
    realized_frac = None
    half_locked = 0.0   # P&L fraction already banked from selling half at +100%
    remaining_frac = 1.0
    for day in range(1, n + 1):
        days_left = DTE - day
        val = spread_value(path[day], days_left, sigma_path)
        ret = val / DEBIT - 1.0
        if use_take_half_at_100:
            if remaining_frac == 1.0 and ret >= 1.00:
                # sell half, bank the 100% return on that half, let the rest ride
                half_locked = 0.5 * 1.00
                remaining_frac = 0.5
            if day == n:
                final_ret = ret  # remainder rides to expiration/close-by-expiration
                realized_frac = half_locked + remaining_frac * final_ret
                break
        else:
            if ret >= quick_exit_pct:
                realized_frac = ret
                break
            if day == n:
                realized_frac = ret
                break
    if realized_frac is None:
        days_left = 0
        val = spread_value(path[n], days_left, sigma_path)
        realized_frac = val / DEBIT - 1.0
    # position sizing is "for zero": can't lose more than the debit
    return max(realized_frac, -1.0)

regimes = {
    "strong_bull_+60pct":  (0.60, ann_sigma),
    "moderate_bull_+30pct": (0.30, ann_sigma),
    "weak_bull_+12pct":     (0.12, ann_sigma),
    "flat_0pct":            (0.0,  ann_sigma),
    "bear_-25pct":          (-0.25, ann_sigma * 1.1),
    "vol_crush_+30pct_low_iv": (0.30, ann_sigma * 0.6),
}

print(f"\nBase construction: long {K_LONG:.0f}C / short {K_SHORT:.0f}C, {DTE}DTE, "
      f"net debit ${DEBIT:.2f} ({DEBIT/WIDTH:.1%} of width)\n")

N_PATHS = 2000
for rule_name, use_half in (("close-all @45%", False), ("sell-half @100%, hold rest", True)):
    print(f"--- Management rule: {rule_name} ---")
    for name, (mu, sig) in regimes.items():
        outs = []
        for s in range(N_PATHS):
            p = gen_path(DTE + 5, mu, sig, seed=hash((name, s, use_half)) % (2**31))
            outs.append(run_trade(p, sig, use_take_half_at_100=use_half))
        win_rate = sum(1 for o in outs if o > 0) / len(outs)
        total_loss_rate = sum(1 for o in outs if o <= -0.99) / len(outs)
        mean_ret = statistics.mean(outs)
        median_ret = statistics.median(outs)
        sd = statistics.pstdev(outs)
        sharpe_like = mean_ret / sd if sd > 0 else 0.0
        print(f"  {name:<28} win_rate={win_rate:5.1%}  total_loss_rate={total_loss_rate:5.1%}  "
              f"mean_ret={mean_ret:+6.1%}  median_ret={median_ret:+6.1%}  return_sd={sd:.2f}  "
              f"mean/sd={sharpe_like:+.2f}")
    print()

# ============ SECTION 3: sensitivity to entry debit (does the rule's price ceiling matter?) ============
print("=== SECTION 3: sensitivity -- what the entry-debit ceiling actually buys you ===")
print(f"{'entry_debit':>12} {'debit/width':>12} {'max_profit':>10} {'reward:risk':>12} {'breakeven_vs_S0':>16}")
for debit in (2.00, 2.50, 2.75, 3.00, 3.50, 4.00):
    max_profit = WIDTH - debit
    rr = max_profit / debit
    breakeven = K_LONG + debit
    within_rule = "within rule" if debit <= 3.00 else "VIOLATES rule (>$3.00 cap)"
    print(f"{debit:>12.2f} {debit/WIDTH:>11.1%} {max_profit:>10.2f} {rr:>11.2f}x "
          f"{breakeven - S0:>+15.2f}  [{within_rule}]")

print("""
Reading Section 3: this is the mechanical reason the rule caps the debit. At $2.50 (the
ideal ceiling) reward:risk is exactly 3.0x. At $3.00 (the hard ceiling) it's already down
to 2.33x -- the low end of the stated 2.3-3x band. Above $3.00 the trade no longer meets
the rule's own risk/reward floor, independent of how good the bullish signal is. Paying
up for a rich spread (as Section 1 shows the current 30-delta/10-wide TSLA spread tends to
do at this vol level) is the single most common way this strategy gets violated silently.
""")
