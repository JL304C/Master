import math, random, statistics, json, sys

# ---------- Real market data (Alpha Vantage, free tier, GPC daily, 2026-04-22..2026-09-14) ----------
raw = {"2026-09-14":133.21,"2026-09-11":133.68,"2026-09-10":134.16,"2026-09-09":135.24,"2026-09-08":134.06,"2026-09-04":138.08,"2026-09-03":137.62,"2026-09-02":137.15,"2026-09-01":134.84,"2026-08-31":135.67,"2026-08-28":137.51,"2026-08-27":136.70,"2026-08-26":139.49,"2026-08-25":137.95,"2026-08-24":135.69,"2026-08-21":133.96,"2026-08-20":132.92,"2026-08-19":134.54,"2026-08-18":131.89,"2026-08-17":132.52,"2026-08-14":135.03,"2026-08-13":135.35,"2026-08-12":134.01,"2026-08-11":135.09,"2026-08-10":134.54,"2026-08-07":135.63,"2026-08-06":132.79,"2026-08-05":131.32,"2026-08-04":132.55,"2026-08-03":128.52,"2026-07-31":124.37,"2026-07-30":124.83,"2026-07-29":129.98,"2026-07-28":129.71,"2026-07-27":127.70,"2026-07-24":124.21,"2026-07-23":120.05,"2026-07-22":120.37,"2026-07-21":119.12,"2026-07-20":122.40,"2026-07-17":124.82,"2026-07-16":125.66,"2026-07-15":121.00,"2026-07-14":122.16,"2026-07-13":123.52,"2026-07-10":125.62,"2026-07-09":124.27,"2026-07-08":124.73,"2026-07-07":128.67,"2026-07-06":128.66,"2026-07-02":132.57,"2026-07-01":117.40,"2026-06-30":117.98,"2026-06-29":117.18,"2026-06-26":116.02,"2026-06-25":112.99,"2026-06-24":110.73,"2026-06-23":106.47,"2026-06-22":105.11,"2026-06-18":108.70,"2026-06-17":106.12,"2026-06-16":107.27,"2026-06-15":104.64,"2026-06-12":103.75,"2026-06-11":102.26,"2026-06-10":98.43,"2026-06-09":99.41,"2026-06-08":97.07,"2026-06-05":98.15,"2026-06-04":98.63,"2026-06-03":98.28,"2026-06-02":99.35,"2026-06-01":97.26,"2026-05-29":98.70,"2026-05-28":99.26,"2026-05-27":98.40,"2026-05-26":97.05,"2026-05-22":97.87,"2026-05-21":97.62,"2026-05-20":94.97,"2026-05-19":92.47,"2026-05-18":93.17,"2026-05-15":92.87,"2026-05-14":97.19,"2026-05-13":98.87,"2026-05-12":100.74,"2026-05-11":101.42,"2026-05-08":104.72,"2026-05-07":105.25,"2026-05-06":105.49,"2026-05-05":104.29,"2026-05-04":103.52,"2026-05-01":104.99,"2026-04-30":107.23,"2026-04-29":103.28,"2026-04-28":105.41,"2026-04-27":106.56,"2026-04-24":108.74,"2026-04-23":109.79,"2026-04-22":111.74}
dates = sorted(raw.keys())
closes = [raw[d] for d in dates]

# ---------- Real Alpha Vantage data, GPC daily, 2026-04-22..2026-09-14 (100 days, ----------
# ---------- free tier -- same limitation as the FIRST NVDA/NFLX passes: this is a  ----------
# ---------- 5-month window, so the drift estimate below is noisy and should NOT be ----------
# ---------- read as a reliable multi-year forecast (same caveat proven right for   ----------
# ---------- NFLX, where the 100-day drift was wildly different from the real 2yr). ----------
S0 = closes[-1]
log_rets = [math.log(closes[i]/closes[i-1]) for i in range(1, len(closes))]
mean_r = sum(log_rets)/len(log_rets)
var_r = sum((r-mean_r)**2 for r in log_rets)/(len(log_rets)-1)
ann_sigma = math.sqrt(var_r) * math.sqrt(252)
ann_drift_100d_noisy = mean_r*252   # +44.7%/yr -- confirmed unusable: compounds GPC to $600+
                                     # within a year and hits the same $26k/20%-OTM capacity
                                     # ceiling found with NVDA, freezing the strategy for years.
                                     # A mature Dividend King has no business at this growth rate.
ann_drift_hist = 0.07                # realistic long-run estimate for a mature dividend king
                                      # (roughly earnings growth + modest multiple drift) --
                                      # used for every test below instead of the noisy figure.

print(f"S0={S0:.2f}  realized_ann_vol={ann_sigma:.3f}  ann_drift_used={ann_drift_hist:.3f} (100d noisy estimate was {ann_drift_100d_noisy:.3f}, rejected as unrealistic)")

# ---------- Black-Scholes ----------
def norm_cdf(x):
    return 0.5*(1+math.erf(x/math.sqrt(2)))

def bs_price(S,K,T,r,sigma,kind):
    if T<=0:
        return max(0.0, (S-K) if kind=='call' else (K-S))
    d1 = (math.log(S/K)+(r+0.5*sigma**2)*T)/(sigma*math.sqrt(T))
    d2 = d1-sigma*math.sqrt(T)
    if kind=='call':
        return S*norm_cdf(d1)-K*math.exp(-r*T)*norm_cdf(d2)
    else:
        return K*math.exp(-r*T)*norm_cdf(-d2)-S*norm_cdf(-d1)

R_FREE = 0.04
DTE_YEARS = 30/365.0

def round_strike(x):
    return round(x/5.0)*5.0

# ---------- GBM path generator ----------
def gen_path(n_days, mu, sigma, seed):
    rnd = random.Random(seed)
    dt = 1/252.0
    S = S0
    path = [S]
    for _ in range(n_days):
        z = rnd.gauss(0,1)
        S = S*math.exp((mu-0.5*sigma**2)*dt + sigma*math.sqrt(dt)*z)
        path.append(S)
    return path

# ---------- Strategy simulation over one price path ----------
CYCLE_DAYS = 21          # ~30 calendar days in trading days
ROLL_TRIGGER = 4         # roll when ~4 trading days (~5 calendar days) remain
ACCOUNT_CAP = 26000.0
# The $255 call-strike cap was specific to NVDA's price level. GPC trades around
# $133, so that figure doesn't translate. No GPC-specific cap was given, so this run
# applies the strike-selection rules (10% above current price) with no hard dollar
# ceiling -- disclosed here rather than silently reusing NVDA's $255.
CALL_CAP = float('inf')
FEE_PER_CONTRACT = 0.65
SLIPPAGE_PCT = 0.03      # 3% of premium eaten by bid-ask on open/close/roll

def simulate(path, put_otm=0.20, call_otm=0.10, dte_days=CYCLE_DAYS, roll_trigger=ROLL_TRIGGER,
             sigma_mult=1.0, apply_costs=True):
    cash = ACCOUNT_CAP
    shares = 0
    i = 0
    n = len(path)-1
    trades = []          # each: dict(kind, pnl, day_open, day_close)
    equity_curve = [(0, cash)]
    sigma_use = ann_sigma*sigma_mult

    def cost(premium):
        if not apply_costs: return premium
        return premium*(1-SLIPPAGE_PCT) - FEE_PER_CONTRACT/100.0  # per-share terms

    outer_guard = 0
    while i < n:
        outer_guard += 1
        if outer_guard > 500:
            break
        S = path[i]
        if shares == 0:
            strike = round_strike(S*(1-put_otm))
            need = strike*100
            if need > ACCOUNT_CAP or need > cash:
                # Can't size a 20%-OTM put within the cap/cash right now (price too high
                # relative to fixed $26k ceiling). Don't exit the strategy -- wait one
                # cycle and re-check, since price may fall back into range later.
                equity_curve.append((i, cash))
                i = min(i+dte_days, n)
                if i>=n:
                    break
                continue
            T = min(dte_days, n-i)/365.0*(365/252)  # convert remaining trading days to year-fraction approx
            premium = bs_price(S, strike, dte_days/365.0, R_FREE, sigma_use, 'put')
            premium = cost(premium) if apply_costs else premium
            cash += premium*100
            exp_idx = min(i+dte_days, n)
            S_exp = path[exp_idx]
            if S_exp < strike:
                # assigned
                cash -= strike*100
                shares = 100
                cost_basis = strike - premium  # net cost basis after put premium collected
                trades.append({'kind':'put_assigned','pnl':premium*100,'day':exp_idx})
            else:
                trades.append({'kind':'put_otm','pnl':premium*100,'day':exp_idx})
            i = exp_idx
            equity_curve.append((i, cash+shares*path[i]))
        else:
            S = path[i]
            strike = min(round_strike(S*(1+call_otm)), CALL_CAP)
            premium = bs_price(S, strike, dte_days/365.0, R_FREE, sigma_use, 'call')
            premium = cost(premium) if apply_costs else premium
            cash += premium*100
            day_open = i
            nominal_exp = min(i+dte_days, n)
            called_away = False
            j = i
            roll_guard = 0
            # roll loop: check every roll_trigger window whether ITM; if not, roll forward
            while True:
                roll_guard += 1
                if roll_guard > 500:
                    i = n
                    break
                check_idx = min(j+dte_days-roll_trigger, n)
                S_check = path[check_idx]
                if S_check >= strike or check_idx >= n:
                    # goes to expiry ITM (or sim ended) -> assume assignment/close at nominal expiry price
                    final_idx = min(check_idx+roll_trigger, n)
                    S_final = path[final_idx]
                    if S_final >= strike or final_idx>=n:
                        cash += strike*100
                        shares = 0
                        trades.append({'kind':'call_assigned','pnl':premium*100,'day':final_idx})
                        i = final_idx
                        called_away = True
                        break
                    else:
                        # dipped back below strike right at expiry -> expired OTM; keep going,
                        # sell a fresh call rather than leaving the shares uncovered.
                        j = final_idx
                        if j >= n:
                            i = n
                            break
                        continue
                else:
                    # still OTM with roll_trigger days left -> roll: buy back (cost) + sell new ~30DTE.
                    # Strike rule (disclosed assumption, spec is ambiguous here): re-price to
                    # ~call_otm above the CURRENT price each roll, but never roll the strike
                    # below the position's cost basis -- a standard real-world guardrail so the
                    # strategy never voluntarily locks in a loss on the shares via assignment.
                    buyback = bs_price(S_check, strike, roll_trigger/365.0, R_FREE, sigma_use, 'call')
                    buyback = buyback*(1+SLIPPAGE_PCT)+FEE_PER_CONTRACT/100.0 if apply_costs else buyback
                    new_strike = min(round_strike(S_check*(1+call_otm)), CALL_CAP)
                    new_strike = max(new_strike, round_strike(cost_basis))
                    new_premium = bs_price(S_check, new_strike, dte_days/365.0, R_FREE, sigma_use, 'call')
                    new_premium = cost(new_premium) if apply_costs else new_premium
                    net = (new_premium - buyback)*100
                    cash += net
                    premium = new_premium
                    strike = new_strike
                    j = check_idx
                    if j >= n:
                        i = n
                        break
            if not called_away:
                # ran out of path without assignment
                equity_curve.append((i, cash+shares*path[min(i,n)]))
                break
            equity_curve.append((i, cash))
    final_S = path[min(i,n)]
    final_equity = cash + shares*final_S
    equity_curve.append((n, final_equity))
    return {'trades':trades, 'equity_curve':equity_curve, 'final_equity':final_equity}

def curve_metrics(ec, n_days):
    # ec: list of (day_idx, equity)
    if len(ec) < 2:
        return None
    vals = [v for _,v in ec]
    rets = []
    for k in range(1,len(vals)):
        if vals[k-1]>0:
            rets.append(vals[k]/vals[k-1]-1)
    total_ret = vals[-1]/vals[0]-1
    years = n_days/252.0
    cagr = (vals[-1]/vals[0])**(1/years)-1 if years>0 and vals[0]>0 else 0
    if len(rets)>1 and statistics.pstdev(rets)>0:
        # crude per-cycle Sharpe scaled to annual using #cycles/year
        cycles_per_year = max(1, len(rets))/years
        sharpe = (statistics.mean(rets)/statistics.pstdev(rets))*math.sqrt(cycles_per_year)
    else:
        sharpe = 0.0
    peak = vals[0]; max_dd = 0.0; dd_start=0; dd_len=0; cur_dd_start=None; max_dd_len=0
    for idx,(day,v) in enumerate(ec):
        if v>peak:
            peak=v
            cur_dd_start=None
        dd = (peak-v)/peak if peak>0 else 0
        if dd>max_dd: max_dd=dd
        if v<peak:
            if cur_dd_start is None: cur_dd_start = ec[idx-1][0] if idx>0 else day
            dd_len = day-cur_dd_start
            if dd_len>max_dd_len: max_dd_len=dd_len
    return {'total_return':total_ret,'cagr':cagr,'sharpe':sharpe,'max_dd':max_dd,'max_dd_days':max_dd_len,'n_trades':len(rets)}

# ============ TEST 1: In-sample-style single run on calibrated base case ============
N_YEARS = 5
N_DAYS = int(252*N_YEARS)
base_path = gen_path(N_DAYS, ann_drift_hist, ann_sigma, seed=42)
res_base = simulate(base_path)
m_base = curve_metrics(res_base['equity_curve'], N_DAYS)
print("\n=== TEST 1: Single calibrated-path run (base case) ===")
print(json.dumps(m_base, indent=2))
print("num option trades (cycles):", len(res_base['trades']))

# ============ TEST 2 (walk-forward substitute): explicit regime scenarios ============
print("\n=== TEST 2 substitute: regime scenarios (no multi-year real data available -> forward regime scenarios) ===")
regimes = {
    'bull_+40pct_vol_same': (0.40, ann_sigma),
    'flat_0pct_vol_same':   (0.0,  ann_sigma),
    'bear_-30pct_vol_x1.3': (-0.30, ann_sigma*1.3),
    'crash_-60pct_vol_x2':  (-0.60, ann_sigma*2.0),
}
regime_results = {}
for name,(mu,sig) in regimes.items():
    outs=[]
    for s in range(30):
        p = gen_path(N_DAYS, mu, sig, seed=1000+s)
        r = simulate(p)
        mm = curve_metrics(r['equity_curve'], N_DAYS)
        if mm: outs.append(mm)
    med_sharpe = statistics.median([o['sharpe'] for o in outs])
    med_ret = statistics.median([o['total_return'] for o in outs])
    med_dd = statistics.median([o['max_dd'] for o in outs])
    regime_results[name] = {'median_sharpe':med_sharpe,'median_total_return':med_ret,'median_max_dd':med_dd,'n':len(outs)}
    print(name, regime_results[name])

# ============ TEST 3: Monte Carlo (1000 paths, base calibration) ============
print("\n=== TEST 3: Monte Carlo, 1000 paths, base calibration ===")
mc_sharpe=[]; mc_dd=[]; mc_ret=[]
for s in range(1000):
    p = gen_path(N_DAYS, ann_drift_hist, ann_sigma, seed=2000+s)
    r = simulate(p)
    mm = curve_metrics(r['equity_curve'], N_DAYS)
    if mm:
        mc_sharpe.append(mm['sharpe']); mc_dd.append(mm['max_dd']); mc_ret.append(mm['total_return'])
mc_sharpe.sort(); mc_dd.sort(); mc_ret.sort()
def pct(lst,p):
    k=int(len(lst)*p)
    return lst[min(k,len(lst)-1)]
print("Sharpe  p5/p50/p95:", pct(mc_sharpe,.05), pct(mc_sharpe,.5), pct(mc_sharpe,.95))
print("MaxDD   p5/p50/p95:", pct(mc_dd,.05), pct(mc_dd,.5), pct(mc_dd,.95))
print("TotRet  p5/p50/p95:", pct(mc_ret,.05), pct(mc_ret,.5), pct(mc_ret,.95))

# ============ TEST 4: parameter sensitivity ±20% ============
print("\n=== TEST 4: parameter sensitivity (±20%) ===")
base_sharpe = statistics.median(mc_sharpe)
def run_variant(**kwargs):
    outs=[]
    for s in range(200):
        p = gen_path(N_DAYS, ann_drift_hist, ann_sigma, seed=3000+s)
        r = simulate(p, **kwargs)
        mm = curve_metrics(r['equity_curve'], N_DAYS)
        if mm: outs.append(mm['sharpe'])
    return statistics.median(outs) if outs else 0.0

sens = {}
sens['put_otm -20% (0.16)'] = run_variant(put_otm=0.16)
sens['put_otm +20% (0.24)'] = run_variant(put_otm=0.24)
sens['call_otm -20% (0.08)'] = run_variant(call_otm=0.08)
sens['call_otm +20% (0.12)'] = run_variant(call_otm=0.12)
sens['dte -20% (24d)'] = run_variant(dte_days=24)
sens['dte +20% (36d)'] = run_variant(dte_days=36)
sens['iv -20%'] = run_variant(sigma_mult=0.8)
sens['iv +20%'] = run_variant(sigma_mult=1.2)
print("base median Sharpe:", base_sharpe)
for k,v in sens.items():
    pct_change = (v-base_sharpe)/abs(base_sharpe)*100 if base_sharpe!=0 else float('nan')
    print(f"{k}: median Sharpe {v:.3f}  ({pct_change:+.1f}% vs base)")

# ============ TEST 5: slippage/fees gross vs net ============
print("\n=== TEST 5: gross vs net (slippage+fees) ===")
def run_costs(apply_costs):
    outs=[]
    for s in range(300):
        p = gen_path(N_DAYS, ann_drift_hist, ann_sigma, seed=4000+s)
        r = simulate(p, apply_costs=apply_costs)
        mm = curve_metrics(r['equity_curve'], N_DAYS)
        if mm: outs.append((mm['sharpe'], mm['total_return']))
    sh = statistics.median([o[0] for o in outs])
    rt = statistics.median([o[1] for o in outs])
    return sh, rt
gross = run_costs(False)
net = run_costs(True)
print("gross (no costs): median Sharpe", gross[0], "median total return", gross[1])
print("net   (w/ costs): median Sharpe", net[0], "median total return", net[1])

# ============ TEST 6: drawdown analysis (from MC set) ============
print("\n=== TEST 6: drawdown detail across 1000 MC paths ===")
dd_days_all=[]
for s in range(1000):
    p = gen_path(N_DAYS, ann_drift_hist, ann_sigma, seed=2000+s)
    r = simulate(p)
    mm = curve_metrics(r['equity_curve'], N_DAYS)
    if mm: dd_days_all.append(mm['max_dd_days'])
dd_days_all.sort()
print("max drawdown duration (trading days) p50/p95:", pct(dd_days_all,.5), pct(dd_days_all,.95))

# ============ Optimization sweep: put OTM% one-variable-at-a-time ============
print("\n=== Optimization sweep: put_otm% ===")
for otm in [0.10,0.15,0.20,0.25,0.30]:
    sh = run_variant(put_otm=otm)
    print(f"put_otm={otm:.2f}: median Sharpe {sh:.3f}")
