import json, math, random, statistics
from datetime import date, timedelta

# ---------- Real market data (Alpha Vantage TIME_SERIES_DAILY, free tier, fetched 2026-09-19) ----------
SYMBOLS = {
    "TSLA": ("data/tsla_daily_2026-09-18.json", None),   # earnings: 2026-10-28 (EARNINGS_CALENDAR)
    "NVDA": ("data/nvda_daily_2026-09-18.json", None),   # EARNINGS_CALENDAR returned no date in next 3mo
    "MSFT": ("data/msft_daily_2026-09-18.json", None),   # EARNINGS_CALENDAR call failed (bad response) -- not verified, disclosed
}
NVDA_EARNINGS_NOTE = "not found in next-3mo Alpha Vantage EARNINGS_CALENDAR call"
MSFT_EARNINGS_NOTE = "EARNINGS_CALENDAR call returned a malformed/error response -- NOT independently verified, check manually before entry"
TSLA_EARNINGS = date(2026, 10, 28)
TODAY = date(2026, 9, 19)

# ---------- New rules being tested (per user request, on top of the original two paragraphs) ----------
IV_ENTRY_FLOOR = 0.35          # only take the trade if vol (IV proxy) at entry is > 35% annualized
THESIS_BROKEN_POINTS = 10.0    # close the whole spread immediately if underlying trades this far below entry

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
FEE_PER_CONTRACT = 0.65
SLIPPAGE_PCT = 0.04
DTE = 30  # base case, matching the TSLA-only audit

def find_30delta_strike(S, T, r, sigma, target=0.30):
    lo, hi = S, S * 1.6
    for _ in range(60):
        mid = (lo + hi) / 2
        d = bs_call_delta(S, mid, T, r, sigma)
        if d > target:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2)

def gen_path(S0, n_days, mu, sigma, seed):
    rnd = random.Random(seed)
    dt = 1 / 252.0
    S = S0
    path = [S]
    for _ in range(n_days):
        z = rnd.gauss(0, 1)
        S = S * math.exp((mu - 0.5 * sigma ** 2) * dt + sigma * math.sqrt(dt) * z)
        path.append(S)
    return path

def run_trade(path, S0, K_long, K_short, debit, sigma_path, use_take_half_at_100):
    """Exit priority each day: (1) thesis-broken stop -- immediate, unconditional close,
    (2) profit-taking rule, (3) close at expiration. 'Never add/roll a loser' means once
    the thesis-broken stop fires there is no re-entry within this trade."""
    n = min(DTE, len(path) - 1)
    half_locked = 0.0
    remaining_frac = 1.0

    def spread_value(S, days_left):
        T = max(days_left, 0) / 365.0
        if T <= 0:
            return max(0.0, min(S, K_short) - K_long)
        return bs_call(S, K_long, T, R_FREE, sigma_path) - bs_call(S, K_short, T, R_FREE, sigma_path)

    for day in range(1, n + 1):
        S = path[day]
        days_left = DTE - day
        val = spread_value(S, days_left)
        ret = val / debit - 1.0

        # (1) thesis-broken stop -- unconditional, checked first, overrides profit logic
        if S <= S0 - THESIS_BROKEN_POINTS:
            realized = half_locked + remaining_frac * ret if remaining_frac < 1.0 else ret
            return max(realized, -1.0)

        # (2) profit-taking
        if use_take_half_at_100:
            if remaining_frac == 1.0 and ret >= 1.00:
                half_locked = 0.5 * 1.00
                remaining_frac = 0.5
        else:
            if ret >= 0.45:
                return max(ret, -1.0)

        # (3) close by expiration
        if day == n:
            final_ret = half_locked + remaining_frac * ret if remaining_frac < 1.0 else ret
            return max(final_ret, -1.0)
    return -1.0

regimes = {
    "strong_bull_+60pct":      (0.60, 1.00),
    "moderate_bull_+30pct":    (0.30, 1.00),
    "weak_bull_+12pct":        (0.12, 1.00),
    "flat_0pct":               (0.0,  1.00),
    "bear_-25pct":             (-0.25, 1.10),
    "vol_crush_+30pct_low_iv": (0.30, 0.60),
}

N_PATHS = 2000
print(f"Rules under test: entry requires vol > {IV_ENTRY_FLOOR:.0%} (IV proxy = trailing realized vol); "
      f"thesis-broken stop = close immediately if price <= entry - {THESIS_BROKEN_POINTS:.0f} pts\n")

summary_rows = []
for symbol, (path_file, _) in SYMBOLS.items():
    with open(path_file) as f:
        raw_ts = json.load(f)["Time Series (Daily)"]
    dts = sorted(raw_ts.keys())
    closes = [float(raw_ts[d]["4. close"]) for d in dts]
    S0 = closes[-1]
    log_rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    mean_r = sum(log_rets) / len(log_rets)
    var_r = sum((r - mean_r) ** 2 for r in log_rets) / (len(log_rets) - 1)
    ann_sigma = math.sqrt(var_r) * math.sqrt(252)

    print(f"=== {symbol}: S0=${S0:.2f}  trailing-100d realized vol (IV proxy) = {ann_sigma:.1%} ===")
    if ann_sigma <= IV_ENTRY_FLOOR:
        print(f"  ENTRY FILTER: FAIL -- {ann_sigma:.1%} is at or below the {IV_ENTRY_FLOOR:.0%} floor. "
              f"No trade would be taken on {symbol} under this rule; skipping Monte Carlo.\n")
        summary_rows.append((symbol, S0, ann_sigma, None, None, None, None))
        continue

    T = DTE / 365.0
    K_long = find_30delta_strike(S0, T, R_FREE, ann_sigma)
    K_short = K_long + WIDTH
    c_long = bs_call(S0, K_long, T, R_FREE, ann_sigma)
    c_short = bs_call(S0, K_short, T, R_FREE, ann_sigma)
    debit = c_long - c_short
    debit = debit * (1 + SLIPPAGE_PCT) + FEE_PER_CONTRACT / 100.0
    ratio = debit / WIDTH
    rr = (WIDTH - debit) / debit
    debit_ok = ratio <= 0.30 and debit <= 3.00

    exp_date = TODAY + timedelta(days=DTE)
    earn_note = ""
    if symbol == "TSLA" and exp_date >= TSLA_EARNINGS:
        earn_note = "  [spans confirmed 2026-10-28 earnings]"
    elif symbol == "NVDA":
        earn_note = f"  [earnings: {NVDA_EARNINGS_NOTE}]"
    elif symbol == "MSFT":
        earn_note = f"  [earnings: {MSFT_EARNINGS_NOTE}]"

    print(f"  ENTRY FILTER: PASS ({ann_sigma:.1%} > {IV_ENTRY_FLOOR:.0%} floor)")
    print(f"  30DTE construction: long {K_long:.0f}C / short {K_short:.0f}C, debit ${debit:.2f} "
          f"({ratio:.1%} of width), reward:risk {rr:.2f}x -> {'PASS' if debit_ok else 'FAIL'} debit-ceiling rule{earn_note}")
    print(f"  Thesis-broken stop level: ${S0 - THESIS_BROKEN_POINTS:.2f} ({THESIS_BROKEN_POINTS/S0:.1%} below spot)")

    row_results = {}
    for rule_name, use_half in (("close-all @45%", False), ("sell-half @100%", True)):
        print(f"  --- {rule_name} (with thesis-broken stop) ---")
        for name, (mu, sig_mult) in regimes.items():
            sig = ann_sigma * sig_mult
            outs = []
            for s in range(N_PATHS):
                p = gen_path(S0, DTE + 5, mu, sig, seed=hash((symbol, name, s, use_half)) % (2**31))
                outs.append(run_trade(p, S0, K_long, K_short, debit, sig, use_half))
            win_rate = sum(1 for o in outs if o > 0) / len(outs)
            stop_rate = sum(1 for o in outs if o <= -0.99) / len(outs)
            mean_ret = statistics.mean(outs)
            sd = statistics.pstdev(outs)
            print(f"    {name:<28} win_rate={win_rate:5.1%}  total_loss_rate={stop_rate:5.1%}  "
                  f"mean_ret={mean_ret:+6.1%}  return_sd={sd:.2f}")
            row_results[(rule_name, name)] = (win_rate, mean_ret, stop_rate)
    print()
    summary_rows.append((symbol, S0, ann_sigma, debit, ratio, rr, row_results))

# ---------- Cross-ticker summary ----------
print("=== Cross-ticker summary (entry filter + construction) ===")
print(f"{'symbol':<6} {'S0':>8} {'vol(IV proxy)':>14} {'entry_pass':>11} {'debit':>7} {'debit/width':>12} {'reward:risk':>12}")
for symbol, S0, ann_sigma, debit, ratio, rr, _ in summary_rows:
    passed = ann_sigma > IV_ENTRY_FLOOR
    if debit is None:
        print(f"{symbol:<6} {S0:>8.2f} {ann_sigma:>13.1%} {'NO':>11} {'--':>7} {'--':>12} {'--':>12}")
    else:
        print(f"{symbol:<6} {S0:>8.2f} {ann_sigma:>13.1%} {'YES':>11} {debit:>7.2f} {ratio:>11.1%} {rr:>11.2f}x")

print("\n=== Cross-ticker summary: 'flat' regime, close-all @45% rule, with thesis-broken stop ===")
print(f"{'symbol':<6} {'win_rate':>10} {'mean_ret':>10} {'total_loss_rate':>16}")
for symbol, S0, ann_sigma, debit, ratio, rr, row_results in summary_rows:
    if row_results is None:
        continue
    wr, mr, sr = row_results[("close-all @45%", "flat_0pct")]
    print(f"{symbol:<6} {wr:>9.1%} {mr:>+9.1%} {sr:>15.1%}")
