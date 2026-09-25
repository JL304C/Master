"""
NVDA delayed iron condor -- historical backtest.

Strategy (user spec, with the vague parts turned into testable rules):
  1. Open a put credit spread (~45 DTE) when NVDA is DECLINING but FINDING SUPPORT.
  2. With 2-4 weeks left, IF NVDA has risen / is near resistance / overbought AND no
     earnings before expiration, add a call credit spread: same expiry, same width,
     same (or partial) contract count, short call delta < 0.20.
  3. Close whichever spread gets threatened (price touches its short strike).
  4. Never hold through earnings (base case).

Data limits (disclosed, not hidden):
  - Alpha Vantage free tier: full DAILY history and historical OPTION CHAINS are premium.
    So this runs on real split-adjusted WEEKLY bars (nvda_weekly_adjusted.csv) and prices
    options with Black-Scholes using realized volatility + an implied-vol premium + a
    put skew. Real fills will differ; the direction of each assumption is noted below.
  - Weekly bars mean entries/adjustments happen on Friday closes. A "touch" of a short
    strike is detected from the week's high/low and the spread is closed as if NVDA were
    exactly at the strike (or at the open if it gapped through).

Dollar scaling: every trade is rescaled so NVDA = $TODAY_PRICE at entry. That makes the
spread widths ($5 / $20) and per-contract costs mean the same thing they would today.
"""
import csv, math, statistics, sys, os
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
TODAY_PRICE = 224.58          # NVDA close, week ending 2026-09-24
START = date(2012, 1, 1)      # backtest window start (~14.7 years)

# ---------------- data ----------------
def load_weekly():
    rows = []
    with open(os.path.join(HERE, 'nvda_weekly_adjusted.csv')) as f:
        for r in csv.DictReader(f):
            d = date.fromisoformat(r['timestamp'])
            c, a = float(r['close']), float(r['adjusted close'])
            k = a / c                       # split/dividend adjustment factor
            rows.append(dict(d=d, o=float(r['open'])*k, h=float(r['high'])*k,
                             l=float(r['low'])*k, c=a))
    rows.sort(key=lambda x: x['d'])
    return rows

def load_earnings():
    out = []
    with open(os.path.join(HERE, 'nvda_earnings_dates.txt')) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                out.append(date.fromisoformat(line))
    return sorted(out)

W = load_weekly()
EARN = load_earnings()

def earnings_between(d0, d1):
    """Any earnings report strictly after d0 and on/before d1 (reports are post-market,
    so a report ON the expiry date still lands before expiry settlement risk)."""
    return any(d0 < e <= d1 for e in EARN)

# ---------------- indicators (weekly) ----------------
closes = [w['c'] for w in W]
def sma(i, n):
    return sum(closes[i-n+1:i+1])/n if i >= n-1 else None
def rsi(i, n=14):
    if i < n: return None
    g = l = 0.0
    for k in range(i-n+1, i+1):
        ch = closes[k]-closes[k-1]
        if ch > 0: g += ch
        else: l -= ch
    if l == 0: return 100.0
    return 100 - 100/(1+g/l)
def realized_vol(i, n=26):
    if i < n: return None
    r = [math.log(closes[k]/closes[k-1]) for k in range(i-n+1, i+1)]
    return statistics.stdev(r)*math.sqrt(52)

# ---------------- pricing ----------------
R = 0.04
def ncdf(x): return 0.5*(1+math.erf(x/math.sqrt(2)))
def bs(S, K, T, sig, kind):
    if T <= 1e-6:
        return max(0.0, S-K) if kind == 'call' else max(0.0, K-S)
    d1 = (math.log(S/K)+(R+0.5*sig*sig)*T)/(sig*math.sqrt(T)); d2 = d1-sig*math.sqrt(T)
    if kind == 'call': return S*ncdf(d1)-K*math.exp(-R*T)*ncdf(d2)
    return K*math.exp(-R*T)*ncdf(-d2)-S*ncdf(-d1)
def bs_delta(S, K, T, sig, kind):
    d1 = (math.log(S/K)+(R+0.5*sig*sig)*T)/(sig*math.sqrt(T))
    return ncdf(d1) if kind == 'call' else ncdf(d1)-1

# Implied-vol model. Assumptions (tunable, tested in sensitivity section):
IV_PREMIUM = 1.10   # equity IV typically trades ~10% above subsequent realized vol
PUT_SKEW   = 0.80   # OTM put IV rises ~0.8 vol-pts per 1% OTM (relative), NVDA-typical
CALL_SKEW  = 0.20   # OTM calls: mild smile
def iv(S, K, atm):
    m = math.log(K/S)
    adj = -PUT_SKEW*m if m < 0 else CALL_SKEW*m
    return max(0.15, atm*(1+adj))

# ---------------- costs (Alpaca) ----------------
# Alpaca: $0 option commission; pass-through regulatory/ORF/OCC fees ~ $0.03/contract.
FEE_PER_CONTRACT = 0.03
# Bid/ask: NVDA options are very liquid; assume we give up $0.03/leg or 2% of leg price,
# whichever is larger, on every open and close.
def leg_slip(px): return max(0.03, 0.02*px)

def strike_round(x, inc, down=True):
    return (math.floor(x/inc) if down else math.ceil(x/inc))*inc

# ---------------- signals ----------------
def entry_signal(i, cfg):
    """DECLINING but FINDING SUPPORT, all on weekly bars:
       declining : the last 2 weeks' low is >= cfg.pullback below the 5-week closing high
       support   : nearest of 10w SMA (~50d), 20w SMA (~100d), 40w SMA (~200d),
                   or prior swing low (lowest low of weeks i-12..i-3, ~60d excl. last 2w)
                   that sits at/below the close; close must be within cfg.near of it
       holding   : the last 2 weeks' low tested support (within +/-3%) and held, AND
                   the week closed up OR closed in the upper half of its range
    """
    if i < 45: return None
    c = closes[i]; w = W[i]
    hi = max(closes[i-5:i+1])             # 5-week (~25 trading day) closing high
    lo2 = min(W[i]['l'], W[i-1]['l'])     # lowest trade of the last 2 weeks
    if lo2 > hi*(1-cfg['pullback']): return None
    levels = [sma(i,10), sma(i,20), sma(i,40), min(x['l'] for x in W[i-12:i-2])]
    below = [lv for lv in levels if lv and lv <= c]
    if not below: return None
    sup = max(below)                      # nearest support underneath the close
    if (c-sup)/sup > cfg['near']: return None
    # tested and held: the last 2 weeks' low came within 3% of support but did not break
    # it by more than 3% (price was rejected there) ...
    if not (sup*0.97 <= lo2 <= sup*1.03): return None
    # ... and the selling is stalling: closed up on the week, or in the upper half of
    # the week's range (a weekly "hammer")
    rng = w['h']-w['l']
    stalling = c >= closes[i-1] or (rng > 0 and (c-w['l'])/rng >= 0.5)
    if not stalling: return None
    return sup

def call_add_signal(i, i0):
    """Risen / near resistance / overbought."""
    c = closes[i]
    risen = c > closes[i0]*1.02
    near_res = c >= 0.97*max(x['h'] for x in W[max(0,i-10):i+1])
    ob = (rsi(i) or 0) >= 65
    return risen or near_res or ob

# ---------------- simulation ----------------
def run(cfg):
    trades = []
    i = 45
    while W[i]['d'] < START: i += 1
    n = len(W)
    while i < n-1:
        sup = entry_signal(i, cfg) if cfg['use_signal'] else closes[i]*0.95
        if sup is None: i += 1; continue
        d0 = W[i]['d']
        # expiry: Friday ~45 DTE (6 or 7 weeks). Earnings rule: if a report lands before
        # expiry, shorten to the last weekly expiry before the report if >= 4 weeks out
        # (still a valid 30+ DTE trade), otherwise skip this signal.
        exp_w = cfg['weeks']
        if not cfg['allow_earnings']:
            while exp_w >= 4 and earnings_between(d0, d0+timedelta(weeks=exp_w)):
                exp_w -= 1
            if exp_w < 4: i += 1; continue
        ie = i+exp_w
        if ie >= n: break
        scale = TODAY_PRICE/closes[i]            # normalise to today's price level
        S = closes[i]*scale
        atm = realized_vol(i)*IV_PREMIUM
        T = exp_w*7/365
        Wd = cfg['width']; inc = 5.0 if Wd >= 5 else 2.5
        # short put below support (2% buffer), and never above 0.30 delta
        Ks = strike_round(sup*scale*0.98, inc)
        while -bs_delta(S, Ks, T, iv(S,Ks,atm), 'put') > 0.30: Ks -= inc
        Kl = Ks-Wd
        p_short = bs(S,Ks,T,iv(S,Ks,atm),'put'); p_long = bs(S,Kl,T,iv(S,Kl,atm),'put')
        put_credit = (p_short-leg_slip(p_short)) - (p_long+leg_slip(p_long))
        if put_credit < cfg['min_credit_pct']*Wd: i += 1; continue   # not worth it
        ctr = cfg['contracts']
        t = dict(entry=d0, exp=W[ie]['d'], S0=S, Ks=Ks, Kl=Kl, weeks=exp_w,
                 put_credit=put_credit, call_credit=0.0, call_ctr=0, put_close=None,
                 call_close=None, fees=FEE_PER_CONTRACT*2*ctr)
        put_open = True; call_open = False; Kc = Kcl = None
        for j in range(i+1, ie+1):
            w = W[j]; o,h,l,c = (w['o']*scale, w['h']*scale, w['l']*scale, w['c']*scale)
            Tj = (ie-j)*7/365
            atm_j = realized_vol(j)*IV_PREMIUM
            # --- threatened-side management (touch of short strike -> close spread) ---
            if put_open and l <= Ks and j < ie:
                Sx = min(o, Ks)
                cost = (bs(Sx,Ks,Tj+3/365,iv(Sx,Ks,atm_j),'put')+leg_slip(1)) - \
                       max(0,bs(Sx,Kl,Tj+3/365,iv(Sx,Kl,atm_j),'put')-leg_slip(1))
                t['put_close'] = min(cost, Wd); put_open = False
                t['fees'] += FEE_PER_CONTRACT*2*ctr
            if call_open and h >= Kc and j < ie:
                Sx = max(o, Kc)
                cost = (bs(Sx,Kc,Tj+3/365,iv(Sx,Kc,atm_j),'call')+leg_slip(1)) - \
                       max(0,bs(Sx,Kcl,Tj+3/365,iv(Sx,Kcl,atm_j),'call')-leg_slip(1))
                t['call_close'] = min(cost, Wd); call_open = False
                t['fees'] += FEE_PER_CONTRACT*2*t['call_ctr']
            # --- delayed call side: 2-4 weeks remaining ---
            weeks_left = ie-j
            if (cfg['add_calls'] and not call_open and t['call_ctr'] == 0 and put_open
                    and 2 <= weeks_left <= 4 and call_add_signal(j, i)
                    and not earnings_between(w['d'], W[ie]['d'])):
                K = strike_round(c*1.01, inc, down=False)
                while bs_delta(c, K, Tj, iv(c,K,atm_j), 'call') >= cfg['call_delta']: K += inc
                Kc, Kcl = K, K+Wd
                cs = bs(c,Kc,Tj,iv(c,Kc,atm_j),'call'); cl = bs(c,Kcl,Tj,iv(c,Kcl,atm_j),'call')
                cc = (cs-leg_slip(cs))-(cl+leg_slip(cl))
                if cc >= 0.05:                       # skip if the call credit is trivial
                    t['call_credit'] = cc; t['Kc'] = Kc
                    t['call_ctr'] = max(1, int(round(ctr*cfg['call_frac'])))
                    t['fees'] += FEE_PER_CONTRACT*2*t['call_ctr']
                    call_open = True
        Sexp = closes[ie]*scale
        if put_open:  t['put_close'] = min(Wd, max(0, Ks-Sexp))
        if call_open: t['call_close'] = min(Wd, max(0, Sexp-Kc))
        t['Sexp'] = Sexp
        put_pnl = (t['put_credit']-t['put_close'])*100*ctr
        call_pnl = (t['call_credit']-(t['call_close'] or 0))*100*t['call_ctr']
        t['pnl'] = put_pnl+call_pnl-t['fees']
        t['put_pnl'] = put_pnl; t['call_pnl'] = call_pnl
        t['bp'] = (Wd-t['put_credit'])*100*ctr      # buying power reserved (call side shares it)
        t['max_loss'] = (Wd*100*ctr) - (t['put_credit']*100*ctr + t['call_credit']*100*t['call_ctr'])
        t['ret_on_bp'] = t['pnl']/t['bp']
        trades.append(t)
        i = ie+1 if not cfg['overlap'] else i+1       # one position at a time (base)
    return trades

def summarize(name, trades, years):
    if not trades:
        print(f"{name}: no trades"); return None
    pnl = [t['pnl'] for t in trades]; r = [t['ret_on_bp'] for t in trades]
    wins = sum(1 for p in pnl if p > 0)
    added = [t for t in trades if t['call_ctr']]
    # equity curve on a fixed account, 1 position at a time, fixed contract count
    eq = 0; peak = 0; mdd = 0
    for p in pnl:
        eq += p; peak = max(peak, eq); mdd = max(mdd, peak-eq)
    weeks_in = sum(t['weeks'] for t in trades)
    avg_bp = statistics.mean(t['bp'] for t in trades)
    out = dict(n=len(trades), win=wins/len(trades), total=sum(pnl), avg=statistics.mean(pnl),
               worst=min(pnl), best=max(pnl), avg_ret_bp=statistics.mean(r),
               calls_added=len(added), call_pnl=sum(t['call_pnl'] for t in trades),
               mdd=mdd, avg_bp=avg_bp, per_year=sum(pnl)/years,
               ann_on_bp=(sum(pnl)/years)/avg_bp, time_in_mkt=weeks_in/(years*52))
    print(f"{name:<44} n={out['n']:>3} win={out['win']:.0%} total=${out['total']:>8,.0f} "
          f"avg=${out['avg']:>6,.0f} worst=${out['worst']:>7,.0f} maxDD=${out['mdd']:>7,.0f} "
          f"$/yr={out['per_year']:>6,.0f} ann/BP={out['ann_on_bp']:>6.1%} "
          f"callsAdded={out['calls_added']} callPnL=${out['call_pnl']:,.0f} inMkt={out['time_in_mkt']:.0%}")
    return out

BASE = dict(pullback=0.07, near=0.06, weeks=6, width=20.0, contracts=1, add_calls=True,
            call_delta=0.20, call_frac=1.0, allow_earnings=False, use_signal=True,
            min_credit_pct=0.10, overlap=False)

if __name__ == '__main__':
    yrs = (W[-1]['d']-START).days/365.25
    print(f"NVDA weekly bars {START}..{W[-1]['d']} ({yrs:.1f} yrs), prices rescaled to ${TODAY_PRICE}, 1 contract\n")
    res = {}
    for wd in (5.0, 20.0):
        print(f"--- spread width ${wd:.0f} ---")
        c = dict(BASE, width=wd)
        res[(wd,'put')]   = summarize("A) put spread only (signal)", run(dict(c, add_calls=False)), yrs)
        res[(wd,'ic')]    = summarize("B) delayed iron condor (full call add)", run(c), yrs)
        res[(wd,'half')]  = summarize("C) delayed IC, calls on 50% of contracts", run(dict(c, contracts=2, call_frac=0.5)), yrs)
        res[(wd,'nosig')] = summarize("D) no signal: put spread 5% OTM every cycle", run(dict(c, use_signal=False, add_calls=False)), yrs)
        res[(wd,'nosigic')] = summarize("E) no signal + delayed call add", run(dict(c, use_signal=False)), yrs)
        res[(wd,'earn')]  = summarize("F) delayed IC, HOLD through earnings", run(dict(c, allow_earnings=True)), yrs)
        print()

    print("--- sensitivity (width $20, delayed IC) ---")
    global_iv = (IV_PREMIUM, PUT_SKEW)
    for lab, prem, skew in [("IV premium 1.00 (IV = realized)",1.00,PUT_SKEW),
                            ("IV premium 1.20",1.20,PUT_SKEW),
                            ("put skew 0.4 (flatter)",IV_PREMIUM,0.4),
                            ("put skew 1.2 (steeper)",IV_PREMIUM,1.2)]:
        IV_PREMIUM, PUT_SKEW = prem, skew
        summarize(lab, run(BASE), yrs)
    IV_PREMIUM, PUT_SKEW = global_iv
    for lab, kw in [("pullback 5%",dict(pullback=0.05)),("pullback 10%",dict(pullback=0.10)),
                    ("near-support 4%",dict(near=0.04)),("near-support 8%",dict(near=0.08)),
                    ("call delta < 0.15",dict(call_delta=0.15)),("call delta < 0.25",dict(call_delta=0.25)),
                    ("7-week expiry",dict(weeks=7))]:
        summarize(lab, run(dict(BASE, **kw)), yrs)

    print("\n--- by period (width $20, delayed IC) ---")
    tr = run(BASE)
    for a,b in [(2012,2015),(2016,2019),(2020,2022),(2023,2026)]:
        sub = [t for t in tr if a <= t['entry'].year <= b]
        if sub:
            print(f"{a}-{b}: n={len(sub)} win={sum(t['pnl']>0 for t in sub)/len(sub):.0%} "
                  f"total=${sum(t['pnl'] for t in sub):,.0f}")

    if '--trades' in sys.argv:
        print("\n--- trade log (width $20, delayed IC) ---")
        for t in tr:
            print(f"{t['entry']} -> {t['exp']} ({t['weeks']}w) S0={t['S0']:.0f} put {t['Ks']:.0f}/{t['Kl']:.0f} "
                  f"cr={t['put_credit']:.2f} close={t['put_close']:.2f} | call "
                  f"{('%.0f'%t['Kc']) if t['call_ctr'] else '-':>4} cr={t['call_credit']:.2f} "
                  f"close={(t['call_close'] or 0):.2f} | Sexp={t['Sexp']:.0f} pnl=${t['pnl']:,.0f}")
