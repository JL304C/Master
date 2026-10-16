import pandas as pd, numpy as np, glob, math, json, sys
from scipy.optimize import brentq
from scipy.stats import norm

daily = pd.read_parquet("cache/daily.parquet")
COMM = 0.65          # per contract (moomoo-ish; Alpaca options are $0)
TARGET = 83.0

NVDA_EARN = pd.to_datetime("""2018-05-10 2018-08-16 2018-11-15 2019-02-14 2019-05-16 2019-08-15 2019-11-14 2020-02-13
2020-05-21 2020-08-19 2020-11-18 2021-02-24 2021-05-26 2021-08-18 2021-11-17 2022-02-16 2022-05-25 2022-08-24 2022-11-16
2023-02-22 2023-05-24 2023-08-23 2023-11-21 2024-02-21 2024-05-22 2024-08-28 2024-11-20 2025-02-26 2025-05-28 2025-08-27
2025-11-19 2026-02-25 2026-05-20 2026-08-26""".split())

def bs(S,K,T,s,cp):
    if T<=0 or s<=0: return max(0,(S-K) if cp=="C" else (K-S))
    d1=(math.log(S/K)+0.5*s*s*T)/(s*math.sqrt(T)); d2=d1-s*math.sqrt(T)
    return S*norm.cdf(d1)-K*norm.cdf(d2) if cp=="C" else K*norm.cdf(-d2)-S*norm.cdf(-d1)
def iv(px,S,K,T,cp):
    try: return brentq(lambda s: bs(S,K,T,s,cp)-px, 1e-3, 10)
    except ValueError: return np.nan

def load(f):
    q = pd.read_parquet(f)
    q = q[q.symbol.str.startswith("SOXL  ")].copy()
    s = q.symbol.str[6:]
    q["exp"] = pd.to_datetime("20"+s.str[:6]); q["cp"]=s.str[6]; q["K"]=s.str[7:].astype(int)/1000
    q = q.dropna(subset=["bid_px_00","ask_px_00"])
    q = q[(q.ask_px_00>0)&(q.ask_px_00>=q.bid_px_00)]
    q["mid"]=(q.bid_px_00+q.ask_px_00)/2
    return q

snaps = {}
for f in sorted(glob.glob("cache/q_*.parquet")):
    d = pd.Timestamp(f[-16:-8])
    q = load(f)
    if len(q): snaps[d]=q

def em_for(q, S, exp, d):
    T = max((exp-d).days,0.5)/365
    e = q[q.exp==exp]
    ks = sorted(set(e[e.cp=="C"].K)&set(e[e.cp=="P"].K))
    if not ks: return np.nan, np.nan
    k = min(ks, key=lambda k: abs(k-S))
    c = e[(e.cp=="C")&(e.K==k)].mid.iloc[0]; p = e[(e.cp=="P")&(e.K==k)].mid.iloc[0]
    v = np.nanmean([iv(c,S,k,T,"C"), iv(p,S,k,T,"P")])
    return S*v*math.sqrt((exp-d).days/365)*0.798, v

MODE = dict(lo=4, hi=10, tgt=7, rlo=10, rhi=18)
def pick_exps(q, d):
    m = MODE
    exps = sorted(e for e in q.exp.unique() if e>d)
    dte = {e:(e-d).days for e in exps}
    tr = [e for e in exps if m["lo"]<=dte[e]<=m["hi"]]
    if not tr: return None, None
    t = min(tr, key=lambda e: abs(dte[e]-m["tgt"]))
    rf = [e for e in exps if m["rlo"]<=dte[e]<=m["rhi"] and e>t]
    if not rf: return t, None
    return t, min(rf, key=lambda e: abs(dte[e]-2*dte[t]))

def choose(chain, target, side, rounding, prem_rule):
    """side 'P': OTM below target; 'C': above. returns row or None"""
    ks = np.array(sorted(chain.K.unique()))
    if not len(ks): return None
    if rounding=="nearest": k = ks[np.argmin(abs(ks-target))]
    elif side=="P":
        c = ks[ks<=target]; k = c.max() if len(c) else None
    else:
        c = ks[ks>=target]; k = c.min() if len(c) else None
    if k is None: return None
    row = chain[chain.K==k].iloc[0]
    if prem_rule=="B" and row.bid_px_00*100 < TARGET:
        i = np.searchsorted(ks,k)
        j = i+1 if side=="P" else i-1
        if 0<=j<len(ks): row = chain[chain.K==ks[j]].iloc[0]
    return row

def price_on(day):
    idx = daily.index[daily.index<=day]
    return daily.close.loc[idx[-1]]

def run(rounding="down", prem_rule="A", em_source="double", wheel=True):
    days = sorted(snaps)
    A = 1.0             # account value (normalized); fractional contracts so capital compounds
    cash, sh, basis = 1.0, 0.0, None
    pos = None          # open short option
    log = []; eq = []
    one = []            # per-1-contract records
    for i,d in enumerate(days):
        q = snaps[d]; S = q.spot.iloc[0]
        if not np.isfinite(S): S = price_on(d)
        # splits between prior mark and now
        if i>0:
            seg = daily.close[(daily.index>days[i-1])&(daily.index<=d)]
            prev = price_on(days[i-1])
            for px in seg:
                r = prev/px
                if r>1.8 or r<0.55:
                    ratio = round(r) if r>1 else 1/round(1/r)
                    sh *= ratio; basis = basis/ratio if basis else basis
                    if pos: pos["K"]/=ratio; pos["n"]*=ratio
                prev = px
        # settle option expiring on/before today
        if pos and pos["exp"]<=d:
            Sx = S if pos["exp"]==d else price_on(pos["exp"])
            K,n = pos["K"],pos["n"]
            assigned = (Sx<K) if pos["cp"]=="P" else (Sx>K)
            rec = dict(open=pos["d"], exp=pos["exp"], cp=pos["cp"], K=K, spot0=pos["S"], spotx=Sx, prem1=pos["prem1"],
                       assigned=assigned, flag=pos["flag"])
            if pos["cp"]=="P":
                rec["pnl1"] = pos["prem1"]-COMM - max(0,K-Sx)*100
                if assigned and wheel: cash -= K*100*n; sh += 100*n; basis = K
                elif assigned: cash -= (K-Sx)*100*n
            else:
                rec["pnl1"] = pos["prem1"]-COMM
                if assigned: cash += K*100*n; sh -= 100*n; rec["share_pnl1"]=(K-basis)*100; basis=None; sh=0 if abs(sh)<1e-9 else sh
            one.append(rec); pos=None
        A = cash + sh*S
        if pos:   # mark open short option at mid (fallback: intrinsic)
            m = q[(q.exp==pos["exp"])&(q.cp==pos["cp"])&(abs(q.K-pos["K"])<1e-6)]
            v = m.mid.iloc[0] if len(m) else max(0,(pos["K"]-S) if pos["cp"]=="P" else (S-pos["K"]))
            A -= v*100*pos["n"]
        eq.append((d,A, cash, sh*S))
        if pos: continue   # (shouldn't happen with weekly)
        t, r = pick_exps(q, d)
        if t is None: continue
        ref = r if em_source=="double" else t
        if ref is None: continue
        em, vol = em_for(q, S, ref, d)
        if not np.isfinite(em): continue
        flag = bool(((NVDA_EARN>d)&(NVDA_EARN<=t)).any()) or any(x.weekday()==4 and x.day<=7 for x in pd.date_range(d+pd.Timedelta(days=1),t))
        if sh<=1e-9:
            ch = q[(q.exp==t)&(q.cp=="P")&(q.bid_px_00>0)]
            row = choose(ch, S-em, "P", rounding, prem_rule)
            if row is None: continue
            n = A/(row.K*100)
        else:
            ch = q[(q.exp==t)&(q.cp=="C")&(q.bid_px_00>0)]
            row = choose(ch, S+em, "C", "up" if rounding=="down" else "nearest", prem_rule)
            if row is None: continue
            n = sh/100
        prem1 = row.bid_px_00*100
        cash += (prem1-COMM)*n
        pos = dict(d=d, exp=t, cp=row.cp, K=row.K, n=n, prem1=prem1, S=S, flag=flag, em=em, vol=vol)
    eq = pd.DataFrame(eq, columns=["d","A","cash","stock"]).set_index("d")
    return eq, pd.DataFrame(one)

def summarize(name, eq, one):
    yrs = (eq.index[-1]-eq.index[0]).days/365.25
    cagr = eq.A.iloc[-1]**(1/yrs)-1
    dd = (eq.A/eq.A.cummax()-1).min()
    puts = one[one.cp=="P"]; calls = one[one.cp=="C"]
    out = dict(variant=name, weeks=len(one), put_trades=len(puts), assignments=int(puts.assigned.sum()),
               assign_rate=puts.assigned.mean(), call_trades=len(calls), called_away=int(calls.assigned.sum()),
               avg_put_prem=puts.prem1.mean(), med_put_prem=puts.prem1.median(), pct_put_ge83=(puts.prem1>=83).mean(),
               avg_put_ror_wk=(puts.prem1/(puts.K*100)).mean(),
               final_x=eq.A.iloc[-1], cagr=cagr, max_dd=dd, worst_put_week_1c=puts.pnl1.min(),
               weeks_in_shares=int((eq.stock>0).sum()))
    return out

def bh(eq):
    c = daily.close[(daily.index>=eq.index[0])&(daily.index<=eq.index[-1])]
    r = c/c.shift(); r[(r>1.8)|(r<0.55)] = np.nan
    adj = r.fillna(1).cumprod()
    yrs=(eq.index[-1]-eq.index[0]).days/365.25
    return dict(final_x=adj.iloc[-1], cagr=adj.iloc[-1]**(1/yrs)-1, max_dd=(adj/adj.cummax()-1).min())

if __name__=="__main__":
    import pickle
    pd.set_option("display.width",250); pd.set_option("display.max_columns",30); pd.set_option("display.float_format",lambda x:f"{x:,.4f}")
    first_weekly = min(d for d,q in snaps.items() if pick_exps(q,d)[1] is not None)
    print("first week with weekly+2wk expirations:", first_weekly.date())
    allsnaps = dict(snaps)
    snaps.clear(); snaps.update({d:q for d,q in allsnaps.items() if d>=first_weekly})
    res=[]; store={}
    for rnd in ["down","nearest"]:
        for pr in ["A","B"]:
            eq,one = run(rnd,pr); n=f"{pr}/{rnd}"; res.append(summarize(n,eq,one)); store[n]=(eq,one)
    eq,one = run("down","A",em_source="single"); res.append(summarize("near-EM A/down",eq,one)); store["near"]=(eq,one)
    print("WEEKLY", first_weekly.date(), "->", eq.index[-1].date())
    print(pd.DataFrame(res).set_index("variant").T)
    print("buy&hold SOXL", bh(store["A/down"][0]))
    one = store["A/down"][1]; p=one[one.cp=="P"]
    print("flagged vs unflagged puts (A/down):"); print(p.groupby("flag").agg(n=("K","size"),assign=("assigned","mean"),prem=("prem1","mean"),pnl1=("pnl1","mean")))
    print("by year (A/down puts):"); print(p.groupby(p.open.dt.year).agg(n=("K","size"),assigned=("assigned","sum"),prem=("prem1","mean")))
    eqA=store["A/down"][0]; print("equity by year-end (A/down):"); print(eqA.A.groupby(eqA.index.year).last())
    # monthly version over full history
    MODE.update(lo=21, hi=45, tgt=30, rlo=45, rhi=75)
    snaps.clear(); snaps.update(allsnaps)
    res2=[]
    for rnd in ["down","nearest"]:
        eq,one = run(rnd,"A"); res2.append(summarize(f"monthly A/{rnd}",eq,one)); store[f"m_{rnd}"]=(eq,one)
    eq,one = run("down","A",em_source="single"); res2.append(summarize("monthly near-EM",eq,one)); store["m_near"]=(eq,one)
    print("MONTHLY", eq.index[0].date(), "->", eq.index[-1].date())
    print(pd.DataFrame(res2).set_index("variant").T)
    print("buy&hold SOXL", bh(store["m_down"][0]))
    em=store["m_down"][0]; print(em.A.groupby(em.index.year).last())
    pickle.dump(store, open("store.pkl","wb"))
