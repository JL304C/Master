"""
Audit of the NQ/MNQ liquidity-sweep scalper (scalper_backtest.py).

Two modes:

  python scalper_audit.py                 # synthetic validation of the ENGINE
  python scalper_audit.py --csv NQ.csv --tz UTC   # the real verdict, on real bars

Why synthetic first: no real 1-minute NQ history was obtainable from this
environment (Alpha Vantage intraday is a premium endpoint; Yahoo is blocked),
and a GBM-style random walk -- the tool used for the wheel audit -- cannot
answer "does this scalp have an edge", because a random walk by construction
has no liquidity-sweep behaviour to exploit.  What synthetic data CAN answer:

  TEST 0  Lookahead check: results for past days must not change when future
          bars are deleted.  Any difference = the engine peeks ahead.
  TEST 1  Null test (random walk, no edge): an honest engine must show ~zero
          gross expectancy and lose roughly its costs.  A strongly positive
          result here would mean the backtest itself is biased.
  TEST 2  Positive control: the same walk with a planted "stop-hunt then
          reversal" effect.  The engine must detect it (positive expectancy),
          proving it can see the edge IF real data contains it.
  TEST 3  Cost drag: $ and R cost per trade = the minimum real edge needed.
  TEST 4  Parameter sensitivity +/-20% (on the positive control).
  TEST 5  Rule variants (entry mode, target rule, HTF filter, session).

With --csv the same battery runs on the real data, plus an in-sample /
out-of-sample split and a bootstrap confidence interval on expectancy.
"""
import math, random, statistics, json, sys, argparse
from dataclasses import replace
from datetime import datetime, timedelta, time as dtime
from multiprocessing import Pool

from scalper_backtest import Bars, Config, run, summarize, load_csv, in_range

# ---------- synthetic NQ-like 1-minute generator ----------
PRICE0 = 24000.0
ANN_VOL = 0.22           # NQ long-run realized vol ~20-25%
TICK = 0.25
SUBSTEPS = 4             # intra-minute path points (for realistic H/L)
T_DF = 4                 # fat-tailed returns

# intraday volatility profile (ET): quiet Asia, London bump, big NY open
PROFILE = [((18, 0), (20, 0), 0.5), ((20, 0), (2, 0), 0.4), ((2, 0), (5, 0), 0.9),
           ((5, 0), (8, 0), 0.6), ((8, 0), (9, 30), 0.9), ((9, 30), (10, 30), 2.2),
           ((10, 30), (12, 0), 1.3), ((12, 0), (14, 0), 0.8), ((14, 0), (16, 0), 1.2),
           ((16, 0), (17, 0), 0.5)]


def _w(t):
    for a, b, w in PROFILE:
        if in_range(t, dtime(*a), dtime(*b)):
            return w
    return 0.5


def _session_minutes():
    start = datetime(2000, 1, 1, 18, 0)
    return [(start + timedelta(minutes=m)).time() for m in range(23 * 60)]


_MIN = _session_minutes()
_WTS = [_w(t) for t in _MIN]
_NORM = math.sqrt(sum(w * w for w in _WTS))
DAILY_SIG = ANN_VOL / math.sqrt(252)
_T_SCALE = math.sqrt(T_DF / (T_DF - 2))


def gen_bars(n_days, seed, edge=0.0):
    """edge > 0 plants a stop-hunt reversal: inside the London/NY windows, when
    a bar trades below the prior 30-bar low (or above the high) by >=2 ticks,
    price gets a drift back the other way for the next 10 minutes of size
    edge x local sigma per substep."""
    rnd = random.Random(seed)
    ts, O, H, L, C = [], [], [], [], []
    px = PRICE0
    day = datetime(2025, 1, 6)  # a Monday
    made = 0
    while made < n_days:
        if day.weekday() < 5:
            s_start = datetime.combine(day - timedelta(days=1), dtime(18, 0))
            boost, boost_left = 0.0, 0
            for m, (t, w) in enumerate(zip(_MIN, _WTS)):
                sig = DAILY_SIG * w / _NORM / math.sqrt(SUBSTEPS)
                o = px; hi = lo = px
                for _ in range(SUBSTEPS):
                    z = rnd.gauss(0, 1) / math.sqrt(rnd.gammavariate(T_DF / 2, 2) / T_DF) / _T_SCALE
                    drift = boost * sig if boost_left > 0 else 0.0
                    px *= math.exp(drift + sig * z)
                    hi = max(hi, px); lo = min(lo, px)
                r = lambda v: round(v / TICK) * TICK
                ts.append(s_start + timedelta(minutes=m))
                O.append(r(o)); H.append(r(hi)); L.append(r(lo)); C.append(r(px))
                px = C[-1]
                if boost_left > 0:
                    boost_left -= 1
                if edge > 0 and len(L) > 31 and (in_range(t, dtime(2, 0), dtime(5, 0)) or in_range(t, dtime(9, 30), dtime(11, 0))):
                    if L[-1] <= min(L[-31:-1]) - 2 * TICK:
                        boost, boost_left = +edge, 10
                    elif H[-1] >= max(H[-31:-1]) + 2 * TICK:
                        boost, boost_left = -edge, 10
            made += 1
        day += timedelta(days=1)
    return Bars(ts, O, H, L, C)


N_DAYS = 250             # one year of sessions per path
N_PATHS = 40
EDGE = 0.35


def _one(args):
    seed, edge, cfg = args
    b = gen_bars(N_DAYS, seed, edge)
    tr, d = run(b, cfg)
    return summarize(tr, d, cfg)


def mc(cfg, edge, seeds, pool):
    return [s for s in pool.map(_one, [(sd, edge, cfg) for sd in seeds]) if s.get('n_trades')]


def pct(xs, p):
    xs = sorted(xs); return xs[min(int(len(xs) * p), len(xs) - 1)]


def dist(res, key):
    xs = [r[key] for r in res]
    return f"p5 {pct(xs, .05):9.2f}  p50 {pct(xs, .5):9.2f}  p95 {pct(xs, .95):9.2f}"


def report_mc(name, res):
    print(f"\n--- {name}: {len(res)} one-year paths ---")
    for k in ('n_trades', 'win_rate', 'expectancy', 'avg_R', 'profit_factor', 'gross_pnl', 'net_pnl',
              'max_dd_usd', 'sharpe_daily'):
        print(f"  {k:14s} {dist(res, k)}")
    prof = sum(1 for r in res if r['net_pnl'] > 0) / len(res)
    print(f"  paths net-profitable: {prof:.0%}")
    return dict(median_expectancy=statistics.median(r['expectancy'] for r in res),
                median_net=statistics.median(r['net_pnl'] for r in res),
                median_sharpe=statistics.median(r['sharpe_daily'] for r in res),
                share_profitable=prof,
                median_trades=statistics.median(r['n_trades'] for r in res))


def lookahead_test():
    print("\n=== TEST 0: lookahead check (truncate / scramble future, past must not change) ===")
    cfg = Config()
    ok, n_cmp = True, 0
    key = lambda t: (t.entry_idx, t.entry, t.init_stop, tuple(t.exits))
    for seed in (7, 8, 9):
        b = gen_bars(150, seed=seed, edge=EDGE)
        full, _ = run(b, cfg)
        days = sorted(set(b.sdate))
        for cut_day in (50, 100, 140):
            target = days[cut_day]
            cut = b.sdate.index(target)          # first bar of that session
            past = [key(t) for t in full if t.day < str(target)]
            part, _ = run(b.slice(cut), cfg)
            rnd = random.Random(cut)
            jig = lambda xs, d: xs[:cut] + [x + d * rnd.choice((1, -1)) for x in xs[cut:]]
            sc = Bars(b.ts, jig(b.o, 40), b.h[:cut] + [x + 60 for x in b.h[cut:]],
                      b.l[:cut] + [x - 60 for x in b.l[cut:]], jig(b.c, 40))
            scr, _ = run(sc, cfg)
            same = past == [key(t) for t in part if t.day < str(target)] == [key(t) for t in scr if t.day < str(target)]
            ok &= same; n_cmp += len(past)
            if not same:
                print(f"  seed {seed} cut {cut_day}: MISMATCH")
    print(f"  compared {n_cmp} trades (3 paths x 3 cut points, truncated AND scrambled futures)")
    print("  RESULT:", "PASS - no lookahead detected" if ok else "FAIL - engine uses future data")
    return ok


def synthetic_audit():
    out = {}
    out['lookahead_ok'] = lookahead_test()
    base = Config()
    with Pool() as pool:
        print(f"\n=== TEST 1: NULL test - random walk, no planted edge (NQ-like vol {ANN_VOL:.0%}) ===")
        null_net = mc(base, 0.0, range(100, 100 + N_PATHS), pool)
        out['null'] = report_mc('null, net of costs', null_net)
        null_gross = mc(replace(base, apply_costs=False), 0.0, range(100, 100 + N_PATHS), pool)
        out['null_gross'] = report_mc('null, gross (no fees/slippage)', null_gross)

        print(f"\n=== TEST 2: POSITIVE CONTROL - planted stop-hunt reversal (edge={EDGE}) ===")
        pos = mc(base, EDGE, range(500, 500 + N_PATHS), pool)
        out['positive'] = report_mc('positive control, net', pos)

        print("\n=== TEST 3: cost drag per trade ===")
        spec = base.spec
        per_trade = [(g['gross_pnl'] - n['net_pnl']) / n['n_trades'] for g, n in zip(null_gross, null_net) if n['n_trades']]
        c = statistics.median(per_trade)
        print(f"  median fees+slippage per trade: ${c:.2f} = {c / base.risk_per_trade:.3f} R "
              f"({spec['rt_fee']:.2f} RT fees/contract + {base.slippage_ticks} tick slip on market fills)")
        print(f"  -> the real-data edge must exceed ~{c / base.risk_per_trade:.2f}R per trade just to break even")
        out['cost_per_trade'] = c

        print("\n=== TEST 4: parameter sensitivity +/-20% (positive-control data, median Sharpe) ===")
        seeds = range(900, 900 + 24)
        b_sh = statistics.median(r['sharpe_daily'] for r in mc(base, EDGE, seeds, pool))
        print(f"  base median Sharpe {b_sh:.2f}")
        sens = {}
        for name, kw in [('disp_mult', 'disp_mult'), ('min_rr', 'min_rr'), ('confirm_bars', 'confirm_bars'),
                         ('eq_tol_ticks', 'eq_tol_ticks'), ('max_hold_bars', 'max_hold_bars')]:
            v0 = getattr(base, kw)
            for mult in (0.8, 1.2):
                v = v0 * mult
                if isinstance(v0, int): v = max(1, round(v))
                else: v = round(v, 2)
                sh = statistics.median(r['sharpe_daily'] for r in mc(replace(base, **{kw: v}), EDGE, seeds, pool))
                sens[f'{name}={v}'] = sh
                print(f"  {name}={v!s:6s} median Sharpe {sh:6.2f} ({(sh - b_sh) / abs(b_sh) * 100 if b_sh else 0:+.0f}%)")
        for mult in (0.8, 1.2):
            cfg = replace(base, london=base.london[:3] + (base.london[3] * mult,), ny=base.ny[:3] + (base.ny[3] * mult,))
            sh = statistics.median(r['sharpe_daily'] for r in mc(cfg, EDGE, seeds, pool))
            print(f"  max_stop x{mult:.1f}   median Sharpe {sh:6.2f} ({(sh - b_sh) / abs(b_sh) * 100 if b_sh else 0:+.0f}%)")
        out['sensitivity'] = sens

        print("\n=== TEST 5: rule variants (positive-control data) ===")
        for name, cfg in [('base (conservative retest, nearest-pool target)', base),
                          ('aggressive entry', replace(base, entry_mode='aggressive')),
                          ('target = first pool >= 2R', replace(base, target_mode='first_beyond')),
                          ('no HTF premium/discount filter', replace(base, htf_filter=False)),
                          ('London only', replace(base, use_ny=False)),
                          ('NY only', replace(base, use_london=False))]:
            r = mc(cfg, EDGE, seeds, pool)
            print(f"  {name:48s} trades/yr {statistics.median(x['n_trades'] for x in r):5.0f}  "
                  f"exp/trade ${statistics.median(x['expectancy'] for x in r):7.2f}  "
                  f"Sharpe {statistics.median(x['sharpe_daily'] for x in r):5.2f}")
    return out


# ---------- real-data battery ----------
def bootstrap_ci(xs, n=2000, seed=0):
    rnd = random.Random(seed)
    ms = sorted(statistics.mean(rnd.choices(xs, k=len(xs))) for _ in range(n))
    return ms[int(n * .025)], ms[int(n * .975)]


def real_audit(path, tz, instrument):
    bars = load_csv(path, tz)
    print(f"loaded {bars.n} bars {bars.ts[0]} .. {bars.ts[-1]} (ET)")
    base = Config(instrument=instrument)
    tr, d = run(bars, base)
    s = summarize(tr, d, base)
    print("\n=== FULL SAMPLE (net of costs) ===")
    print(json.dumps(s, indent=2, default=str))
    if len(tr) >= 10:
        lo, hi = bootstrap_ci([t.pnl for t in tr])
        print(f"95% bootstrap CI of expectancy/trade: ${lo:.2f} .. ${hi:.2f}"
              + ("  (includes zero -> NOT statistically distinguishable from no edge)" if lo <= 0 <= hi else ""))
    g_tr, g_d = run(bars, replace(base, apply_costs=False))
    print(f"\ngross (no costs) net_pnl: {summarize(g_tr, g_d, base).get('gross_pnl', 0):.2f}")

    days = sorted(set(bars.sdate)); half = len(days) // 2
    print("\n=== IN-SAMPLE / OUT-OF-SAMPLE (first half vs second half, same rules) ===")
    for name, ds in (('first half', set(days[:half])), ('second half', set(days[half:]))):
        t2, d2 = run(bars, base, day_filter=ds)
        s2 = summarize(t2, d2, base)
        print(f"  {name}: trades {s2.get('n_trades', 0)}  exp ${s2.get('expectancy', 0):.2f}  "
              f"PF {s2.get('profit_factor', 0):.2f}  net ${s2.get('net_pnl', 0):.0f}  Sharpe {s2.get('sharpe_daily', 0):.2f}")

    print("\n=== sensitivity +/-20% and variants ===")
    variants = [('base', base)]
    for kw in ('disp_mult', 'min_rr', 'confirm_bars', 'eq_tol_ticks', 'max_hold_bars'):
        v0 = getattr(base, kw)
        for m in (0.8, 1.2):
            v = max(1, round(v0 * m)) if isinstance(v0, int) else round(v0 * m, 2)
            variants.append((f'{kw}={v}', replace(base, **{kw: v})))
    variants += [('aggressive entry', replace(base, entry_mode='aggressive')),
                 ('target first>=2R', replace(base, target_mode='first_beyond')),
                 ('no HTF filter', replace(base, htf_filter=False)),
                 ('London only', replace(base, use_ny=False)), ('NY only', replace(base, use_london=False))]
    for name, cfg in variants:
        t2, d2 = run(bars, cfg); s2 = summarize(t2, d2, cfg)
        print(f"  {name:22s} trades {s2.get('n_trades', 0):4d}  win {s2.get('win_rate', 0):.0%}  "
              f"exp ${s2.get('expectancy', 0):7.2f}  PF {s2.get('profit_factor', 0):5.2f}  "
              f"net ${s2.get('net_pnl', 0):8.0f}  maxDD ${s2.get('max_dd_usd', 0):6.0f}")


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--csv'); ap.add_argument('--tz', default='America/New_York')
    ap.add_argument('--instrument', default='MNQ')
    a = ap.parse_args()
    if a.csv:
        real_audit(a.csv, a.tz, a.instrument)
    else:
        res = synthetic_audit()
        print("\nSUMMARY:", json.dumps({k: v for k, v in res.items() if k != 'sensitivity'}, default=str))
