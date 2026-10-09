"""
Backtest of the 1-1-1-2 Put Step-Down Ladder on SPY, 2008-present.

DATA (and why it is modeled, not real option prices):
  - SPY weekly OHLC, real unadjusted prices, Alpha Vantage TIME_SERIES_WEEKLY
    (free tier) -> data/spy_weekly.csv
  - VIX daily close, CBOE history mirrored at github.com/datasets/finance-vix
    -> data/vix_daily.csv
  Alpha Vantage HISTORICAL_OPTIONS (real chains back to 2008) is a PREMIUM
  endpoint and the key in use is free-tier, so option prices are modeled with
  Black-Scholes: 90-day ATM vol derived from that day's real VIX, plus a put
  skew (SKEW_PER_SD in ladder_common.py). Strikes, settlement prices and the
  timing of every crash are real; only option premiums/deltas are modeled.
  Run with --skew to see how sensitive the result is to that assumption.

Entry: every weekly bar's close (Friday, or Thursday in holiday weeks),
closest monthly expiration to 90 DTE. Skip if the fill is not a net credit.
Variants:
  hold      - hold to expiration (the YouTuber's version)
  stop_touch- close the whole ladder the first week SPY's LOW trades below the
              lower breakeven; filled at min(breakeven, week open) and the
              week's highest VIX (vol spikes when this triggers)
  stop_close- same, but only triggers on a weekly CLOSE below breakeven and
              fills at that close (a later, usually worse, exit)
Weekly bars are all the free data allows: a daily stop would usually exit
between these two variants.

Usage: python ladder_backtest.py [--skew 0.30] [--out results]
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import ladder_common as lc

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"

START_ENTRY = date(2007, 10, 1)   # first expirations land in Jan 2008
SLIP_PER_CONTRACT = 0.02          # $/share lost to bid-ask per contract on an mleg fill near mid
SLIP_STRESSED = 0.05              # ... when VIX > 30 (stop exits happen here)
FEE_PER_CONTRACT = 0.05           # regulatory/clearing fees; Alpaca charges no commission
CONTRACTS = sum(abs(r) for r in lc.RATIOS)   # 5 per ladder
DIV_YIELD = 0.017                 # SPY's long-run dividend yield
# Average 3-month T-bill yield by year (approximate, for discounting only).
RATES = {2007: .044, 2008: .014, 2009: .0015, 2010: .0014, 2011: .0005, 2012: .0009,
         2013: .0006, 2014: .0003, 2015: .0005, 2016: .0032, 2017: .0093, 2018: .0194,
         2019: .0206, 2020: .0037, 2021: .0005, 2022: .0202, 2023: .0507, 2024: .0497,
         2025: .042, 2026: .038}
REPORT_YEARS = (2008, 2020, 2022, 2025)


def load_spy():
    rows = []
    with (DATA / "spy_weekly.csv").open() as fh:
        for r in csv.DictReader(fh):
            rows.append((date.fromisoformat(r["timestamp"]), float(r["open"]), float(r["high"]),
                         float(r["low"]), float(r["close"])))
    rows.sort()
    return rows


def load_vix():
    d, v = [], []
    with (DATA / "vix_daily.csv").open() as fh:
        for r in csv.DictReader(fh):
            d.append(date.fromisoformat(r["DATE"]))
            v.append(float(r["CLOSE"]))
    return d, v


class Vix:
    def __init__(self):
        self.d, self.v = load_vix()

    def on(self, day: date) -> float:
        i = bisect.bisect_right(self.d, day) - 1
        return self.v[max(i, 0)]

    def week_max(self, week_end: date) -> float:
        lo = bisect.bisect_left(self.d, week_end - timedelta(days=6))
        hi = bisect.bisect_right(self.d, week_end)
        return max(self.v[lo:hi]) if hi > lo else self.on(week_end)

    @property
    def last(self):
        return self.d[-1]


def rate(day):
    return RATES.get(day.year, 0.03)


def run(skew: float, slip_normal=None, slip_stressed=None, credit_filter=True):
    slip_normal = SLIP_PER_CONTRACT if slip_normal is None else slip_normal
    slip_stressed = SLIP_STRESSED if slip_stressed is None else slip_stressed
    spy = load_spy()
    vix = Vix()
    dates = [r[0] for r in spy]
    last_day = min(dates[-1], vix.last)

    def bar_for_expiry(exp):
        # weekly bar whose week contains the expiration (holiday-safe)
        i = bisect.bisect_left(dates, exp - timedelta(days=3))
        return i if i < len(dates) and dates[i] <= exp + timedelta(days=6) else None

    today_spot = spy[-1][4]
    trades, skipped = [], []
    # weekly mark-to-market P&L contributions per variant: {variant: {bar_idx: pnl}}
    mtm = {v: defaultdict(float) for v in ("hold", "stop_touch", "stop_close")}
    realized_at = {v: defaultdict(float) for v in mtm}

    for i, (d0, o, h, l, S) in enumerate(spy):
        if d0 < START_ENTRY or d0 > last_day:
            continue
        exp = lc.monthly_expiry_near(d0, 90)
        T0 = (exp - d0).days / 365.0
        r, q = rate(d0), DIV_YIELD
        v0 = vix.on(d0)
        atm = lc.atm_vol_from_vix(v0)
        lad = lc.build_ladder(S, T0, r, q, atm, inc=1.0, skew=skew)
        slip = slip_stressed if v0 > 30 else slip_normal
        fill_credit = lad.credit - slip * CONTRACTS          # per share, after crossing spread
        fees = FEE_PER_CONTRACT * CONTRACTS * lc.MULTIPLIER / 100.0
        if credit_filter and fill_credit * 100 <= fees:
            skipped.append({"entry": d0.isoformat(), "spot": S, "vix": v0, "mid_credit": round(lad.credit, 3),
                            "fill_credit": round(fill_credit, 3), "strikes": lad.strikes})
            continue
        lad.credit = fill_credit
        be = lad.breakeven()
        naked_px = lc.bs_put(S, lad.k4, T0, r, q, lc.put_iv(S, lad.k4, T0, atm, skew))
        xi = bar_for_expiry(exp)
        is_open = xi is None or dates[xi] > last_day
        settle = None if is_open else spy[xi][4]

        base = {
            "entry": d0.isoformat(), "expiration": exp.isoformat(), "dte": (exp - d0).days,
            "spot": round(S, 2), "vix": v0, "atm_iv_90d": round(atm, 4),
            "raw_delta_strikes": "/".join(f"{k:g}" for k in lad.raw_strikes),
            "k1_buy": lad.k1, "k2_sell": lad.k2, "k3_buy": lad.k3, "k4_sell2": lad.k4,
            "width": lad.width, "width_pct_spot": round(lad.width / S * 100, 2),
            "credit": round(lad.credit, 3), "max_profit": round(lad.max_profit(), 2),
            "breakeven": round(be, 2), "breakeven_pct_below_spot": round((1 - be / S) * 100, 2),
            **lad.drop_losses(S),
            "cash_secured_bp": round(lad.cash_secured_requirement(), 2),
            "regt_margin_bp": round(lad.regt_requirement(S, naked_px), 2),
            "settle": settle,
        }

        # walk the life of the trade week by week
        last_bar = xi if not is_open else len(spy) - 1
        stopped = {"stop_touch": None, "stop_close": None}
        prev_val = {v: 0.0 for v in mtm}     # P&L already booked into mtm, per variant
        for j in range(i, last_bar + 1):
            dj, oj, hj, lj, cj = spy[j]
            if dj > last_day:
                break
            T = max(0.0, (exp - dj).days / 365.0)
            atm_j = lc.atm_vol_from_vix(vix.on(dj))
            mark = lc.ladder_value(lad, cj, T, rate(dj), q, atm_j, skew) + lad.credit * 100 - fees
            if j == xi:
                mark = lad.payoff(cj) - fees
            for v in mtm:
                if v == "hold" or stopped.get(v) is None:
                    val = mark
                    if v != "hold" and j > i:
                        trig = lj < be if v == "stop_touch" else cj < be
                        if trig and j != xi:
                            if v == "stop_touch":
                                ex_s, ex_vix = min(be, oj), vix.week_max(dj)
                            else:
                                ex_s, ex_vix = cj, vix.on(dj)
                            ex_atm = lc.atm_vol_from_vix(ex_vix)
                            ex_slip = (slip_stressed if ex_vix > 30 else slip_normal) * CONTRACTS * 100
                            val = (lc.ladder_value(lad, ex_s, T, rate(dj), q, ex_atm, skew)
                                   + lad.credit * 100 - fees * 2 - ex_slip)
                            stopped[v] = {"date": dj.isoformat(), "spot": round(ex_s, 2), "vix": ex_vix,
                                          "pnl": round(val, 2)}
                    mtm[v][j] += val - prev_val[v]
                    prev_val[v] = val

        for v in mtm:
            st = stopped.get(v)
            if is_open and st is None:
                status, pnl, closed = "open", prev_val[v], None
            elif st is not None:
                status, pnl, closed = "stopped", st["pnl"], st["date"]
            else:
                status, pnl, closed = "expired", lad.payoff(settle) - fees, dates[xi].isoformat()
            t = dict(base, variant=v, status=status, close_date=closed, pnl=round(pnl, 2),
                     pnl_at_todays_spy_size=round(pnl * today_spot / S, 2),
                     exit_spot=(st or {}).get("spot", settle), exit_vix=(st or {}).get("vix"))
            trades.append(t)
    return trades, skipped, mtm, spy


def drawdown(series):
    peak, mdd, eq = 0.0, 0.0, 0.0
    for x in series:
        eq += x
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)
    return mdd


def stats(trs):
    closed = [t for t in trs if t["status"] != "open"]
    if not closed:
        return {"trades": 0}
    pnl = [t["pnl"] for t in closed]
    wins = [p for p in pnl if p > 0]
    losses = [p for p in pnl if p <= 0]
    csp = sum(t["cash_secured_bp"] for t in closed) / len(closed)
    regt = sum(t["regt_margin_bp"] for t in closed) / len(closed)
    tot = sum(pnl)
    # equity curve ordered by close date, for a per-period drawdown of realized P&L
    by_close = [t["pnl"] for t in sorted(closed, key=lambda t: t["close_date"])]
    return {
        "trades": len(closed),
        "win_rate_pct": round(100 * len(wins) / len(closed), 1),
        "avg_win": round(sum(wins) / len(wins), 2) if wins else 0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0,
        "largest_loss": round(min(pnl), 2),
        # SPY was $70-$130 in 2008; this rescales each trade to today's SPY
        # price so a 2008 loss and a 2025 loss are comparable in dollars
        "largest_loss_at_todays_spy_size": round(min(t["pnl_at_todays_spy_size"] for t in closed), 2),
        "avg_loss_pct_of_cash_secured": round(100 * sum(t["pnl"] / t["cash_secured_bp"] for t in closed if t["pnl"] <= 0)
                                              / max(1, len(losses)), 2),
        "total_pnl": round(tot, 2),
        "avg_pnl_per_ladder": round(tot / len(closed), 2),
        "realized_max_drawdown": round(drawdown(by_close), 2),
        "avg_cash_secured_bp_per_ladder": round(csp),
        "avg_regt_margin_per_ladder": round(regt),
        "return_per_trade_on_cash_secured_pct": round(100 * tot / len(closed) / csp, 3),
        "return_per_trade_on_regt_pct": round(100 * tot / len(closed) / regt, 3),
    }


def loss_episodes(trs):
    """Group losing trades whose expirations are within 45 days of each other."""
    losers = sorted((t for t in trs if t["status"] != "open" and t["pnl"] <= 0), key=lambda t: t["close_date"])
    eps = []
    for t in losers:
        d = date.fromisoformat(t["close_date"])
        if eps and (d - eps[-1]["_last"]).days <= 45:
            e = eps[-1]
        else:
            e = {"first_close": t["close_date"], "losing_trades": 0, "total_loss": 0.0, "worst": 0.0}
            eps.append(e)
        e["_last"] = d
        e["last_close"] = t["close_date"]
        e["losing_trades"] += 1
        e["total_loss"] = round(e["total_loss"] + t["pnl"], 2)
        e["worst"] = round(min(e["worst"], t["pnl"]), 2)
    for e in eps:
        e.pop("_last")
    return eps


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skew", type=float, default=lc.SKEW_PER_SD)
    ap.add_argument("--out", default="results")
    ap.add_argument("--slip", type=float, default=SLIP_PER_CONTRACT)
    ap.add_argument("--slip-stressed", type=float, default=SLIP_STRESSED)
    ap.add_argument("--no-credit-filter", action="store_true",
                    help="take every Friday's ladder even at a debit (shows what the filter avoided)")
    ap.add_argument("--bp-cap", type=float, default=0.20, help="max fraction of equity for this strategy")
    args = ap.parse_args()

    trades, skipped, mtm, spy = run(args.skew, args.slip, args.slip_stressed, not args.no_credit_filter)
    out = HERE / args.out
    out.mkdir(exist_ok=True)

    with (out / "trades.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(trades[0].keys()))
        w.writeheader()
        w.writerows(trades)
    with (out / "skipped_entries.csv").open("w", newline="") as fh:
        if skipped:
            w = csv.DictWriter(fh, fieldnames=list(skipped[0].keys()))
            w.writeheader()
            w.writerows(skipped)

    summary = {"skew_per_sd": args.skew, "slip": args.slip, "slip_stressed": args.slip_stressed,
               "credit_filter": not args.no_credit_filter, "entries_taken": len(trades) // 3, "entries_skipped_no_credit": len(skipped),
               "variants": {}}
    for v in ("hold", "stop_touch", "stop_close"):
        tv = [t for t in trades if t["variant"] == v]
        weekly = [mtm[v].get(j, 0.0) for j in range(len(spy))]
        # peak concurrent capital: sum over ladders open in each week
        conc_cs, conc_rt = defaultdict(float), defaultdict(float)
        for t in tv:
            end = t["close_date"] or "9999"
            for j, row in enumerate(spy):
                ds = row[0].isoformat()
                if t["entry"] <= ds < end:
                    conc_cs[j] += t["cash_secured_bp"]
                    conc_rt[j] += t["regt_margin_bp"]
        s = stats(tv)
        s["max_drawdown_mark_to_market"] = round(drawdown(weekly), 2)
        s["peak_concurrent_cash_secured_bp"] = round(max(conc_cs.values()))
        s["peak_concurrent_regt_margin"] = round(max(conc_rt.values()))
        s["account_needed_at_bp_cap_cash_secured"] = round(s["peak_concurrent_cash_secured_bp"] / args.bp_cap)
        s["account_needed_at_bp_cap_regt"] = round(s["peak_concurrent_regt_margin"] / args.bp_cap)
        yrs = (spy[-1][0] - START_ENTRY).days / 365.25
        avg_cs = sum(conc_cs.values()) / max(1, len(conc_cs))
        avg_rt = sum(conc_rt.values()) / max(1, len(conc_rt))
        s["annualized_return_on_avg_cash_secured_bp_pct"] = round(100 * s["total_pnl"] / yrs / avg_cs, 2)
        s["annualized_return_on_avg_regt_margin_pct"] = round(100 * s["total_pnl"] / yrs / avg_rt, 2)
        s["loss_episodes"] = loss_episodes(tv)
        s["by_expiration_year"] = {y: stats([t for t in tv if t["close_date"] and t["close_date"][:4] == str(y)])
                                   for y in REPORT_YEARS}
        s["open_trades"] = sum(1 for t in tv if t["status"] == "open")
        summary["variants"][v] = s

    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
