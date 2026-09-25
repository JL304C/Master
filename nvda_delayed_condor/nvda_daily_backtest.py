"""
NVDA delayed iron condor -- DAILY-bar backtest (Databento XNAS.ITCH ohlcv-1d, 2018-05..2026-09).

Simulates what nvda_condor_bot.py actually does, day by day:
  - entry signal checked only on the last trading day of each week, on weekly bars built
    from the daily bars up to that day (same condor_rules.entry_signal as the bot)
  - expiry = the Friday closest to 45 DTE (28..52 DTE) with no earnings on/before it
  - every day: close a spread if the day's low/high touches its short strike
  - call spread added on the first day with 14..28 DTE left where call_add_signal is true
    (weekly bars including the current partial week, like the bot) and no earnings
  - one position per cycle: after an early close, wait for the original expiry

Options are still priced with Black-Scholes (no historical option prices yet), using the
same IV model and Alpaca cost assumptions as the weekly backtest. Every trade is rescaled
so NVDA = $224.58 at entry, so $5/$20 widths mean what they mean today.

Data caveat: Databento's daily bars include pre/after-market trading, so a few daily
highs/lows are more extreme than the regular session. That can only trigger extra early
closes (conservative). Earnings-day bars include the after-hours reaction; the strategy
avoids holding through earnings, so that mostly affects variant F.
"""
import csv, math, os, statistics, sys
from datetime import date, timedelta

import condor_rules as rules
import nvda_delayed_condor_backtest as wk      # pricing model + cost assumptions

HERE = os.path.dirname(os.path.abspath(__file__))
TODAY_PRICE = wk.TODAY_PRICE


def load_daily():
    rows = []
    with open(os.path.join(HERE, 'nvda_daily_databento.csv')) as f:
        for r in csv.DictReader(f):
            rows.append(dict(d=date.fromisoformat(r['date']), o=float(r['open']), h=float(r['high']),
                             l=float(r['low']), c=float(r['close'])))
    rows.sort(key=lambda x: x['d'])
    return rows


D = load_daily()
EARN = rules.load_earnings(os.path.join(HERE, 'nvda_earnings_dates.txt'))
closes = [x['c'] for x in D]


def week_key(d):
    return d.isocalendar()[:2]


# weekly bars built from daily bars; WEEK_END_IDX[k] = daily index of week k's last day
WEEKS, WEEK_END_IDX, WEEK_OF_DAY = [], [], []
for i, x in enumerate(D):
    if WEEKS and week_key(WEEKS[-1]['d']) == week_key(x['d']):
        w = WEEKS[-1]
        w['h'] = max(w['h'], x['h']); w['l'] = min(w['l'], x['l']); w['c'] = x['c']; w['d'] = x['d']
        WEEK_END_IDX[-1] = i
    else:
        WEEKS.append(dict(d=x['d'], o=x['o'], h=x['h'], l=x['l'], c=x['c']))
        WEEK_END_IDX.append(i)
    WEEK_OF_DAY.append(len(WEEKS) - 1)


def weekly_upto(i):
    """Weekly bars as the bot sees them on day i: completed weeks + the partial current week."""
    k = WEEK_OF_DAY[i]
    bars = WEEKS[:k]
    start = WEEK_END_IDX[k - 1] + 1 if k > 0 else 0
    part = D[start:i + 1]
    bars = bars + [dict(d=D[i]['d'], o=part[0]['o'], h=max(p['h'] for p in part),
                        l=min(p['l'] for p in part), c=D[i]['c'])]
    return bars


def realized_vol(i, n=126):
    r = [math.log(closes[k] / closes[k - 1]) for k in range(i - n + 1, i + 1)]
    return statistics.stdev(r) * math.sqrt(252)


def pick_expiry(i, allow_earnings):
    """Index of the expiry day: last trading day on/before a Friday 28..52 DTE out,
    closest to 45 DTE, with no earnings on/before it (unless allowed)."""
    d0 = D[i]['d']
    best = None
    for dte in range(28, 53):
        f = d0 + timedelta(days=dte)
        if f.weekday() != 4:
            continue
        if not allow_earnings and rules.earnings_between(EARN, d0, f):
            continue
        if best is None or abs(dte - 45) < abs(best[1] - 45):
            best = (f, dte)
    if best is None:
        return None
    f, dte = best
    if f > D[-1]['d']:
        return None            # the trade the rules pick hasn't finished yet -- no fake early settle
    j = i
    while j + 1 < len(D) and D[j + 1]['d'] <= f:
        j += 1
    return j, dte


def spread_value(S, Kshort, Klong, T, atm, kind, closing):
    """Cost to close (closing=True) a short spread, with bid/ask slippage per leg."""
    ps = wk.bs(S, Kshort, T, wk.iv(S, Kshort, atm), kind)
    pl = wk.bs(S, Klong, T, wk.iv(S, Klong, atm), kind)
    return (ps + wk.leg_slip(ps)) - max(0.0, pl - wk.leg_slip(pl))


def run(width=20.0, contracts=1, add_calls=True, call_frac=1.0, call_delta=0.20,
        allow_earnings=False, use_signal=True, min_credit_pct=0.10, start=date(2018, 11, 1)):
    trades = []
    inc = 5.0
    busy_until = -1
    for k, i in enumerate(WEEK_END_IDX):
        if D[i]['d'] < start or i <= busy_until or i >= len(D) - 1:
            continue
        if use_signal:
            sup = rules.entry_signal(WEEKS, k)
        else:
            sup = closes[i] * 0.95
        if sup is None:
            continue
        ex = pick_expiry(i, allow_earnings)
        if ex is None:
            continue
        ie, dte0 = ex
        scale = TODAY_PRICE / closes[i]
        S = closes[i] * scale
        atm = realized_vol(i) * wk.IV_PREMIUM
        T = dte0 / 365
        Ks = wk.strike_round(sup * scale * 0.98, inc)
        while -wk.bs_delta(S, Ks, T, wk.iv(S, Ks, atm), 'put') > 0.30:
            Ks -= inc
        Kl = Ks - width
        ps = wk.bs(S, Ks, T, wk.iv(S, Ks, atm), 'put'); pl = wk.bs(S, Kl, T, wk.iv(S, Kl, atm), 'put')
        put_credit = (ps - wk.leg_slip(ps)) - (pl + wk.leg_slip(pl))
        if put_credit < min_credit_pct * width:
            continue
        exp_d = D[ie]['d']
        t = dict(entry=D[i]['d'], exp=exp_d, dte=dte0, Ks=Ks, Kl=Kl, put_credit=put_credit,
                 put_close=None, call_credit=0.0, call_close=None, call_ctr=0, Kc=None,
                 fees=wk.FEE_PER_CONTRACT * 2 * contracts, put_exit='expiry', call_exit='')
        put_open, call_open = True, False
        for j in range(i + 1, ie + 1):
            x = D[j]
            o, h, l, c = (x['o'] * scale, x['h'] * scale, x['l'] * scale, x['c'] * scale)
            Tj = max((exp_d - x['d']).days, 0) / 365
            atm_j = realized_vol(j) * wk.IV_PREMIUM
            if put_open and l <= Ks and j < ie:
                Sx = min(o, Ks)
                t['put_close'] = min(width, spread_value(Sx, Ks, Kl, Tj + 0.5 / 365, atm_j, 'put', True))
                t['put_exit'] = f"touch {x['d']}"
                put_open = False; t['fees'] += wk.FEE_PER_CONTRACT * 2 * contracts
            if call_open and h >= t['Kc'] and j < ie:
                Sx = max(o, t['Kc'])
                t['call_close'] = min(width, spread_value(Sx, t['Kc'], t['Kc'] + width, Tj + 0.5 / 365, atm_j, 'call', True))
                t['call_exit'] = f"touch {x['d']}"
                call_open = False; t['fees'] += wk.FEE_PER_CONTRACT * 2 * t['call_ctr']
            dte = (exp_d - x['d']).days
            if (add_calls and put_open and t['call_ctr'] == 0 and 14 <= dte <= 28 and j < ie
                    and not rules.earnings_between(EARN, x['d'], exp_d)
                    and rules.call_add_signal(weekly_upto(j), WEEK_OF_DAY[j], closes[i])):
                Tc = dte / 365
                K = wk.strike_round(c * 1.01, inc, down=False)
                while wk.bs_delta(c, K, Tc, wk.iv(c, K, atm_j), 'call') >= call_delta:
                    K += inc
                cs = wk.bs(c, K, Tc, wk.iv(c, K, atm_j), 'call'); cl = wk.bs(c, K + width, Tc, wk.iv(c, K + width, atm_j), 'call')
                cc = (cs - wk.leg_slip(cs)) - (cl + wk.leg_slip(cl))
                if cc >= 0.05:
                    t['call_credit'] = cc; t['Kc'] = K; t['call_added'] = x['d']
                    t['call_ctr'] = max(1, int(round(contracts * call_frac)))
                    t['fees'] += wk.FEE_PER_CONTRACT * 2 * t['call_ctr']
                    call_open = True; t['call_exit'] = 'expiry'
        Sexp = closes[ie] * scale
        if put_open:
            t['put_close'] = min(width, max(0.0, Ks - Sexp))
        if call_open:
            t['call_close'] = min(width, max(0.0, Sexp - t['Kc']))
        t['Sexp'] = Sexp
        t['put_pnl'] = (t['put_credit'] - t['put_close']) * 100 * contracts
        t['call_pnl'] = (t['call_credit'] - (t['call_close'] or 0)) * 100 * t['call_ctr']
        t['pnl'] = t['put_pnl'] + t['call_pnl'] - t['fees']
        t['bp'] = (width - put_credit) * 100 * contracts
        trades.append(t)
        busy_until = ie
    return trades


def summarize(name, tr, years):
    if not tr:
        print(f"{name:<46} no trades"); return
    p = [t['pnl'] for t in tr]
    eq = peak = mdd = 0
    for x in p:
        eq += x; peak = max(peak, eq); mdd = max(mdd, peak - eq)
    avg_bp = statistics.mean(t['bp'] for t in tr)
    sd = statistics.stdev(p) if len(p) > 1 else 0
    tstat = statistics.mean(p) / (sd / math.sqrt(len(p))) if sd else 0
    added = sum(1 for t in tr if t['call_ctr'])
    print(f"{name:<46} n={len(tr):>3} win={sum(x > 0 for x in p) / len(p):>4.0%} total=${sum(p):>7,.0f} "
          f"avg=${statistics.mean(p):>5,.0f} worst=${min(p):>6,.0f} maxDD=${mdd:>6,.0f} "
          f"$/yr={sum(p) / years:>5,.0f} ret/trade={statistics.mean(p) / avg_bp:>5.1%} t={tstat:>4.1f} "
          f"calls={added} callPnL=${sum(t['call_pnl'] for t in tr):,.0f}")


if __name__ == '__main__':
    start = date(2018, 11, 1)          # first date with 26 weeks of vol + 45 weeks of signal history
    yrs = (D[-1]['d'] - start).days / 365.25
    print(f"NVDA DAILY bars {start}..{D[-1]['d']} ({yrs:.1f} yrs), rescaled to ${TODAY_PRICE}, 1 contract\n")
    for w in (5.0, 20.0):
        print(f"--- width ${w:.0f} ---")
        summarize("A) put spread only (signal)", run(w, add_calls=False), yrs)
        summarize("B) delayed iron condor (as specified)", run(w), yrs)
        summarize("C) calls on 50% (2 put / 1 call)", run(w, contracts=2, call_frac=0.5), yrs)
        summarize("D) no signal: 5% OTM put spread each cycle", run(w, use_signal=False, add_calls=False), yrs)
        summarize("E) no signal + delayed call add", run(w, use_signal=False), yrs)
        summarize("F) delayed IC, hold through earnings", run(w, allow_earnings=True), yrs)
        print()

    # same period, weekly-bar backtest, for comparison
    wk.START = start
    wyrs = (wk.W[-1]['d'] - start).days / 365.25
    print(f"--- weekly-bar backtest, same period ({wyrs:.1f} yrs), width $20 ---")
    wk.summarize("A) put spread only (weekly bars)", wk.run(dict(wk.BASE, add_calls=False)), wyrs)
    wk.summarize("B) delayed iron condor (weekly bars)", wk.run(wk.BASE), wyrs)

    print("\n--- trade log, B) delayed IC, width $20 (daily bars) ---")
    for t in run(20.0):
        call = (f"call {t['Kc']:.0f} added {t['call_added']} cr={t['call_credit']:.2f} "
                f"close={t['call_close']:.2f} ({t['call_exit']})") if t['call_ctr'] else "no call add"
        print(f"{t['entry']} -> {t['exp']} ({t['dte']}d) put {t['Ks']:.0f}/{t['Kl']:.0f} "
              f"cr={t['put_credit']:.2f} close={t['put_close']:.2f} ({t['put_exit']}) | {call} | pnl=${t['pnl']:,.0f}")
