import json, math, random, statistics

# ---------- Improved rule set under test ----------
# 1. Entry vol filter uses TRIMMED realized vol (excludes the single largest |return| in the
#    lookback) instead of raw stdev -- addresses the finding that MSFT's 35%+ reading was an
#    artifact of one earnings-gap day (raw 38.4% vs trimmed 30.9%, which fails the floor).
# 2. Profit-taking defaults to close-all @45% (the data-backed better default from the prior
#    audit) -- sell-half-at-100% is not tested here; treat it as reserved for explicitly
#    high-conviction signals, not the default.
# 3. Stop is now a PERCENTAGE OF ENTRY PRICE, not a flat $10, swept across candidate levels
#    to find where it stops being "constant tax on winners" and starts being "real thesis
#    invalidation."
IV_ENTRY_FLOOR = 0.35
R_FREE = 0.04
WIDTH = 10.0
FEE_PER_CONTRACT = 0.65
SLIPPAGE_PCT = 0.04
DTE = 30

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

def load_symbol(path_file):
    with open(path_file) as f:
        raw_ts = json.load(f)["Time Series (Daily)"]
    dts = sorted(raw_ts.keys())
    closes = [float(raw_ts[d]["4. close"]) for d in dts]
    log_rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    mean_r = sum(log_rets) / len(log_rets)
    n = len(log_rets)
    var_r = sum((r - mean_r) ** 2 for r in log_rets) / (n - 1)
    raw_vol = math.sqrt(var_r) * math.sqrt(252)
    biggest = max(log_rets, key=lambda r: abs(r - mean_r))
    trimmed = [r for r in log_rets if r != biggest]
    mean_t = sum(trimmed) / len(trimmed)
    var_t = sum((r - mean_t) ** 2 for r in trimmed) / (len(trimmed) - 1)
    trimmed_vol = math.sqrt(var_t) * math.sqrt(252)
    return closes[-1], raw_vol, trimmed_vol

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

def run_trade(path, S0, K_long, K_short, debit, sigma_path, stop_level):
    n = min(DTE, len(path) - 1)

    def spread_value(S, days_left):
        T = max(days_left, 0) / 365.0
        if T <= 0:
            return max(0.0, min(S, K_short) - K_long)
        return bs_call(S, K_long, T, R_FREE, sigma_path) - bs_call(S, K_short, T, R_FREE, sigma_path)

    for day in range(1, n + 1):
        S = path[day]
        ret = spread_value(S, DTE - day) / debit - 1.0
        if stop_level is not None and S <= stop_level:
            return max(ret, -1.0)
        if ret >= 0.45:
            return max(ret, -1.0)
        if day == n:
            return max(ret, -1.0)
    return -1.0

regimes = {
    "strong_bull_+60pct":      (0.60, 1.00),
    "moderate_bull_+30pct":    (0.30, 1.00),
    "weak_bull_+12pct":        (0.12, 1.00),
    "flat_0pct":               (0.0,  1.00),
    "bear_-25pct":             (-0.25, 1.10),
    "vol_crush_+30pct_low_iv": (0.30, 0.60),
}

N_PATHS = 1500
STOP_PCTS = [None, 0.02, 0.03, 0.04, 0.05, 0.06]

TICKERS = {
    "TSLA": "data/tsla_daily_2026-09-18.json",
    "NVDA": "data/nvda_daily_2026-09-18.json",
    "MSFT": "data/msft_daily_2026-09-18.json",
}

print("=== Entry filter using TRIMMED realized vol (excludes single largest outlier day) ===")
passing = {}
for symbol, path_file in TICKERS.items():
    S0, raw_vol, trimmed_vol = load_symbol(path_file)
    passed = trimmed_vol > IV_ENTRY_FLOOR
    print(f"  {symbol}: raw_vol={raw_vol:.1%}  trimmed_vol={trimmed_vol:.1%}  "
          f"-> {'PASS' if passed else 'FAIL'} (floor {IV_ENTRY_FLOOR:.0%})")
    if passed:
        passing[symbol] = (S0, trimmed_vol)
print()

for symbol, (S0, sigma) in passing.items():
    T = DTE / 365.0
    K_long = find_30delta_strike(S0, T, R_FREE, sigma)
    K_short = K_long + WIDTH
    c_long = bs_call(S0, K_long, T, R_FREE, sigma)
    c_short = bs_call(S0, K_short, T, R_FREE, sigma)
    debit = c_long - c_short
    debit = debit * (1 + SLIPPAGE_PCT) + FEE_PER_CONTRACT / 100.0

    print(f"=== {symbol}: S0=${S0:.2f}  trimmed_vol={sigma:.1%}  long {K_long:.0f}C/short {K_short:.0f}C  "
          f"debit=${debit:.2f} ({debit/WIDTH:.1%} of width) ===")
    print(f"  {'stop':>10} {'win_rate(6-regime avg)':>24} {'mean_ret(avg)':>15} {'total_loss_rate(avg)':>22}")
    for pct in STOP_PCTS:
        stop_level = S0 * (1 - pct) if pct is not None else None
        label = "none" if pct is None else f"{pct:.0%} (${S0*pct:.2f})"
        wr_all, mr_all, sr_all = [], [], []
        for name, (mu, sig_mult) in regimes.items():
            sig = sigma * sig_mult
            outs = []
            for s in range(N_PATHS):
                p = gen_path(S0, DTE + 5, mu, sig, seed=hash((symbol, name, s, pct)) % (2**31))
                outs.append(run_trade(p, S0, K_long, K_short, debit, sig, stop_level))
            wr_all.append(sum(1 for o in outs if o > 0) / len(outs))
            mr_all.append(statistics.mean(outs))
            sr_all.append(sum(1 for o in outs if o <= -0.99) / len(outs))
        print(f"  {label:>10} {statistics.mean(wr_all):>23.1%} {statistics.mean(mr_all):>+14.1%} "
              f"{statistics.mean(sr_all):>21.1%}")
    print()
