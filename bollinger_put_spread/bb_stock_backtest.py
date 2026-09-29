"""
Stock-side backtest of the Bollinger oversold put credit spread on free daily bars.

Replays the bot's rules (bb_rules.py, the same code the bot runs) day by day:
  signal     close crosses below the 50-day / 2 sd lower band
  entry      next trading morning (the open), 1 contract, max 1 open spread
  expiration nearest standard monthly 45-90 DTE that expires before the next earnings
  strikes    short = highest strike below min(POC, lower band); width ~1% of the close
  exits      checked each day at the close: 2x stop, close below the short strike,
             50% take profit, 21 DTE time stop

Part 1 needs NO option prices: how often the signal fires, how many signals the rules
turn into trades, how far below the price the short strike sits, and how often AMD
closed below the short strike before the 21 DTE time stop.

Part 2 prices the spread with Black-Scholes (IV = recent realized vol x a factor, plus
a put skew) to apply the credit, profit and stop rules. It is a MODEL -- on the NVDA
condor the same kind of model overstated real credits by ~80% -- so read it as a rough
guide; real option prices (Databento OPRA) are the next step.

Both parts are compared with a no-signal baseline: the same rules and the same
distance below the price, entered whenever no spread is open.

Run on the laptop (uses the bot's .env):   python bb_stock_backtest.py
Options:  --symbol AMD  --start 2016-01-01
          --csv bars.csv --earnings dates.txt   (offline: date,open,high,low,close,volume / one date per line)
Writes bb_backtest_trades.csv next to the script.
"""
from __future__ import annotations

import csv
import json
import math
import os
import statistics
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import bb_rules as rules

HERE = Path(__file__).resolve().parent
MIN_CREDIT = 0.50
EXIT_SLIPPAGE = 0.10          # stop/time exits pay ~the natural price: mid + $0.10 on the spread
RISK_FREE = 0.04
SKEW = 0.5                    # put IV = ATM IV x (1 + SKEW x ln(S/K)) for K < S
IV_FACTORS = (0.8, 1.0, 1.2)  # IV = 20-day realized vol x this; 1.0 is the headline
RV_DAYS = 20


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def load_env():
    p = HERE / ".env"
    for cand in (p, HERE / ".env.txt", HERE / "env", HERE / "env.txt"):
        if cand.exists():
            raw = cand.read_bytes()
            text = raw.decode("utf-16") if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else raw.decode("utf-8-sig", "replace")
            for line in text.splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# --------------------------------------------------------------------------- #
# data
# --------------------------------------------------------------------------- #
def bars_from_csv(path):
    out = []
    with open(path) as f:
        for r in csv.DictReader(f):
            r = {k.lower().strip(): v for k, v in r.items()}
            out.append(dict(d=date.fromisoformat(r.get("date") or r.get("timestamp")), o=float(r["open"]),
                            h=float(r["high"]), l=float(r["low"]), c=float(r["close"]), v=float(r["volume"])))
    return sorted(out, key=lambda b: b["d"])


def bars_from_alpaca(symbol, start):
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import Adjustment, DataFeed
    client = StockHistoricalDataClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])
    req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
                           start=datetime.combine(start, datetime.min.time(), timezone.utc),
                           end=datetime.now(timezone.utc) - timedelta(minutes=16),
                           adjustment=Adjustment.ALL, feed=DataFeed.SIP)
    bars = client.get_stock_bars(req)[symbol]
    return [dict(d=b.timestamp.date(), o=float(b.open), h=float(b.high), l=float(b.low),
                 c=float(b.close), v=float(b.volume)) for b in bars]


def earnings_from_alpha_vantage(symbol):
    """Past report dates (EARNINGS) + the next one (EARNINGS_CALENDAR); cached per day."""
    cache = HERE / f"bb_backtest_earnings_{symbol}.json"
    if cache.exists():
        c = json.loads(cache.read_text())
        if c.get("fetched") == date.today().isoformat():
            return [date.fromisoformat(d) for d in c["dates"]]
    key = os.environ.get("ALPHAVANTAGE_API_KEY")
    if not key:
        sys.exit("ALPHAVANTAGE_API_KEY missing from .env")

    def get(**params):
        url = "https://www.alphavantage.co/query?" + urllib.parse.urlencode({**params, "apikey": key})
        with urllib.request.urlopen(url, timeout=60) as r:
            return r.read().decode("utf-8", "replace")

    hist = json.loads(get(function="EARNINGS", symbol=symbol))
    if "quarterlyEarnings" not in hist:
        sys.exit(f"Alpha Vantage EARNINGS failed: {str(hist)[:200]}")
    dates = {date.fromisoformat(q["reportedDate"]) for q in hist["quarterlyEarnings"] if q.get("reportedDate")}
    ok, nxt = rules.parse_earnings_csv(get(function="EARNINGS_CALENDAR", symbol=symbol, horizon="6month"),
                                       symbol, date.today())
    if ok and nxt:
        dates.add(nxt)
    dates = sorted(dates)
    cache.write_text(json.dumps({"fetched": date.today().isoformat(), "dates": [d.isoformat() for d in dates]}))
    return dates


# --------------------------------------------------------------------------- #
# pricing (model only)
# --------------------------------------------------------------------------- #
def ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_put(S, K, T, sig):
    if T <= 0:
        return max(K - S, 0.0)
    sig = max(sig, 0.05)
    d1 = (math.log(S / K) + (RISK_FREE + 0.5 * sig * sig) * T) / (sig * math.sqrt(T))
    d2 = d1 - sig * math.sqrt(T)
    return K * math.exp(-RISK_FREE * T) * ncdf(-d2) - S * ncdf(-d1)


def put_iv(atm, S, K):
    return atm * (1 + SKEW * max(0.0, math.log(S / K)))


def spread_value(S, short, long, T, atm):
    v = bs_put(S, short, T, put_iv(atm, S, short)) - bs_put(S, long, T, put_iv(atm, S, long))
    return min(max(v, 0.0), short - long)


def realized_vol_series(bars, n=RV_DAYS):
    """20-day realized vol at every bar (annualized), precomputed once."""
    closes = [b["c"] for b in bars]
    out = []
    for i in range(len(bars)):
        r = [math.log(closes[k] / closes[k - 1]) for k in range(max(1, i - n + 1), i + 1)]
        out.append(statistics.pstdev(r) * math.sqrt(252) if len(r) > 2 else 0.4)
    return out


# --------------------------------------------------------------------------- #
# listed-strike and expiration approximations
# --------------------------------------------------------------------------- #
def strike_step(k):
    """Typical monthly strike spacing away from the money (AMD's real Nov-2026 chain is $10
    apart around 400). An approximation: real chains vary."""
    return 0.5 if k < 10 else 1.0 if k < 25 else 2.5 if k < 100 else 5.0 if k < 300 else 10.0


def strike_grid(ceiling):
    step = strike_step(ceiling)
    top = math.floor(ceiling / step) * step
    return [round(top - j * step, 2) for j in range(0, 60) if top - j * step > 0][::-1]


def expirations(first: date, last: date, trading: set[date], last_bar: date):
    """{'monthly': 3rd Fridays, 'weekly': every Friday}; a holiday Friday moves to Thursday."""
    def fix(d):
        return d - timedelta(days=1) if d <= last_bar and d not in trading else d
    monthly, y, m = [], first.year, first.month
    while date(y, m, 1) <= last:
        monthly.append(fix(rules.third_friday(y, m)))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    d = first + timedelta(days=(4 - first.weekday()) % 7)
    weekly = []
    while d <= last:
        weekly.append(fix(d))
        d += timedelta(days=7)
    return {"monthly": monthly, "weekly": weekly}


# --------------------------------------------------------------------------- #
# simulation
# --------------------------------------------------------------------------- #
VARIANTS = {
    # name: (entry, strike offset below min(POC, band), (DTE min, max), weeklies allowed)
    "as written":   ("below",   0.00, (45, 90), False),
    "A":            ("reclaim", 0.00, (45, 90), False),
    "B":            ("below",   0.05, (45, 90), False),
    "D":            ("below",   0.00, (30, 60), True),
    "A+B":          ("reclaim", 0.05, (45, 90), False),
    "A+D":          ("reclaim", 0.00, (30, 60), True),
    "B+D":          ("below",   0.05, (30, 60), True),
    "A+B+D":        ("reclaim", 0.05, (30, 60), True),
}


def simulate(bars, earnings, exps, mode, iv_factor, variant, rv, fixed_otm=None):
    """mode 'signal': enter on the variant's signal. mode 'baseline': enter whenever flat,
    short strike fixed_otm below the close (same expiration/width/exit rules).
    variant = (entry 'below'|'reclaim', strike offset, (dte_min, dte_max), weeklies)."""
    entry_kind, offset, (dmin, dmax), weekly = variant
    exp_list = exps["weekly" if weekly else "monthly"]
    signal_fn = rules.cross_below_lower if entry_kind == "below" else rules.cross_above_lower
    closes = [b["c"] for b in bars]
    trades, skips, busy_until = [], {}, -1

    def skip(k):
        skips[k] = skips.get(k, 0) + 1

    start = max(rules.BB_PERIOD + 1, rules.POC_LOOKBACK)
    for i in range(start, len(bars) - 1):
        if mode == "signal":
            hit = signal_fn(bars, i)
            if not hit:
                continue
            if i < busy_until:
                skip("spread already open")
                continue
            poc = rules.volume_profile_poc(bars[:i + 1])
            ceiling_poc, ceiling_band = poc * (1 - offset), hit["lower"] * (1 - offset)
        else:
            if i < busy_until:
                continue
            ceiling_poc = ceiling_band = closes[i] * (1 - fixed_otm)
            poc = hit = None
        e = i + 1                                          # entry the next morning
        ed = bars[e]["d"]
        nxt = next((x for x in earnings if x >= ed), None)
        cands = [x for x in exp_list if dmin <= (x - ed).days <= dmax and (nxt is None or x < nxt)]
        if not cands:
            skip("no expiration before earnings")
            if mode == "baseline":
                busy_until = e
            continue
        exp = cands[0]
        ceiling = min(ceiling_poc, ceiling_band)
        picked = rules.pick_strikes(strike_grid(ceiling), ceiling_poc, ceiling_band, rules.target_width(closes[i]))
        if not picked:
            skip("no strikes")
            continue
        short, long = picked
        S0 = bars[e]["o"]
        credit = round(spread_value(S0, short, long, (exp - ed).days / 365, rv[i] * iv_factor), 2)
        t = dict(mode=mode, signal=bars[i]["d"], entry=ed, expiration=exp, entry_price=S0, short=short, long=long,
                 width=short - long, otm_pct=1 - short / S0, credit=credit, poc=poc,
                 lower=hit["lower"] if hit else None)
        # stock-side (no option prices): first close below the short before the time stop
        breach, d = None, e
        for d in range(e, len(bars)):
            if (exp - bars[d]["d"]).days <= rules.TIME_STOP_DTE:
                break
            if closes[d] < short:
                breach = d
                break
        t["breached_short"] = breach is not None
        t["min_close_vs_short"] = min(closes[k] for k in range(e, (breach if breach is not None else d) + 1)) / short - 1
        if credit < MIN_CREDIT:
            t.update(taken=False, exit_reason=f"model credit {credit:.2f} < {MIN_CREDIT:.2f}")
            trades.append(t)
            if mode == "baseline":
                busy_until = e
            continue
        # model P&L with the bot's exit rules, checked at each close
        exit_i = reason = None
        for d in range(e, len(bars)):
            dte = (exp - bars[d]["d"]).days
            val = spread_value(closes[d], short, long, max(dte, 0) / 365, rv[d] * iv_factor)
            reason = rules.exit_reason(credit, val, closes[d], short, dte)
            if reason:
                exit_i = d
                break
        if exit_i is None:
            t.update(taken=True, exit_reason="still open", exit=None, pnl=None)
            trades.append(t)
            busy_until = len(bars)
            continue
        px = credit * rules.TAKE_PROFIT_FRAC if reason == "take_profit" else min(val + EXIT_SLIPPAGE, short - long)
        t.update(taken=True, exit=bars[exit_i]["d"], exit_reason=reason, exit_price=round(px, 2),
                 pnl=round((credit - px) * 100, 2), days=(bars[exit_i]["d"] - ed).days)
        trades.append(t)
        busy_until = exit_i        # the exit fills at ~3:45 PM; that evening's signal run is free again
    return trades, skips


def stats(trades):
    taken = [t["pnl"] for t in trades if t.get("pnl") is not None]
    out = dict(found=len(trades), breach=sum(t["breached_short"] for t in trades), n=len(taken),
               wins=sum(p > 0 for p in taken), total=sum(taken), worst=min(taken) if taken else 0,
               otm=statistics.median([t["otm_pct"] for t in trades]) if trades else None, dd=0.0, t=0.0)
    eq = peak = 0.0
    for p in taken:
        eq += p
        peak = max(peak, eq)
        out["dd"] = min(out["dd"], eq - peak)
    if len(taken) > 1 and statistics.pstdev(taken):
        out["t"] = statistics.mean(taken) / (statistics.pstdev(taken) / math.sqrt(len(taken)))
    return out


def main():
    load_env()
    symbol = (arg("--symbol") or "AMD").upper()
    start = date.fromisoformat(arg("--start", "2016-01-01"))
    if arg("--csv"):
        bars = bars_from_csv(arg("--csv"))
    else:
        bars = bars_from_alpaca(symbol, start)
    if arg("--earnings"):
        earnings = rules_load_dates(arg("--earnings"))
    else:
        earnings = earnings_from_alpha_vantage(symbol)
    if len(bars) < 200:
        sys.exit(f"only {len(bars)} bars")
    trading = {b["d"] for b in bars}
    exps = expirations(bars[0]["d"], bars[-1]["d"] + timedelta(days=120), trading, bars[-1]["d"])
    print(f"{symbol}: {len(bars)} daily bars {bars[0]['d']} .. {bars[-1]['d']}, "
          f"{sum(1 for e in earnings if bars[0]['d'] <= e <= bars[-1]['d'] + timedelta(days=120))} earnings dates in range")

    rv = realized_vol_series(bars)
    rows, all_trades, details = [], [], {}
    for name, v in VARIANTS.items():
        kind = "cross below band" if v[0] == "below" else "close back above band"
        n_sig = sum(1 for i in range(max(rules.BB_PERIOD + 1, rules.POC_LOOKBACK), len(bars) - 1)
                    if (rules.cross_below_lower if v[0] == "below" else rules.cross_above_lower)(bars, i))
        res = {}
        for f in IV_FACTORS:
            sig, skips = simulate(bars, earnings, exps, "signal", f, v, rv)
            otm = statistics.median([t["otm_pct"] for t in sig]) if sig else 0.05
            base, _ = simulate(bars, earnings, exps, "baseline", f, v, rv, fixed_otm=otm)
            res[f] = (stats(sig), stats(base), skips)
            if f == 1.0:
                details[name] = sig
                for t in sig + base:
                    t["variant"] = name
                all_trades += sig + base
        s1, b1, skips = res[1.0]
        rows.append((name, kind, v, n_sig, skips, s1, b1, res[0.8][0]["total"], res[1.2][0]["total"],
                     res[0.8][1]["total"], res[1.2][1]["total"]))

    print("\nVariants: A = enter when the close gets back above the lower band;"
          " B = short strike 5% below min(POC, band); D = 30-60 DTE, weeklies allowed.")
    print("Stock-side columns need no option prices. Model columns use Black-Scholes (IV = realized x 1.0;"
          " totals at x0.8 / x1.2 in brackets).\n")
    hdr = (f"{'variant':<11}{'signals':>8}{'trades':>7}{'short OTM':>10}{'breached':>9}"
           f"{'model n':>8}{'win':>5}{'total':>9}{'worst':>7}{'maxDD':>8}{'t':>6}   {'IV x0.8 / x1.2':<17}"
           f"{'baseline total':>15}{'base breached':>14}")
    print(hdr)
    print("-" * len(hdr))
    for name, kind, v, n_sig, skips, s1, b1, t08, t12, bt08, bt12 in rows:
        br = f"{s1['breach'] / s1['found']:.0%}" if s1["found"] else "-"
        bbr = f"{b1['breach'] / b1['found']:.0%}" if b1["found"] else "-"
        otm = f"{s1['otm']:.1%}" if s1["otm"] is not None else "-"
        win = f"{s1['wins'] / s1['n']:.0%}" if s1["n"] else "-"
        print(f"{name:<11}{n_sig:>8}{s1['found']:>7}{otm:>10}{br:>9}{s1['n']:>8}{win:>5}"
              f"{s1['total']:>9,.0f}{s1['worst']:>7,.0f}{s1['dd']:>8,.0f}{s1['t']:>6.1f}   "
              f"{f'{t08:,.0f} / {t12:,.0f}':<17}{b1['total']:>15,.0f}{bbr:>14}")
    print("\nSkipped signals per variant:")
    for name, kind, v, n_sig, skips, *_ in rows:
        print(f"  {name:<11}" + (", ".join(f"{k} {c}" for k, c in skips.items()) or "none"))

    for name in ("A+B+D", max(details, key=lambda k: stats(details[k])["total"])):
        print(f"\nSignal trades, {name} (IV x 1.0):")
        for t in details[name]:
            print(f"  {t['signal']} -> {t['entry']} exp {t['expiration']}  {t['short']:g}/{t['long']:g}P "
                  f"({t['otm_pct']:.1%} OTM)  credit~{t['credit']:.2f}  "
                  f"{'BREACHED' if t['breached_short'] else 'held   '}  {t['exit_reason']}"
                  + (f"  ${t['pnl']:,.0f}" if t.get("pnl") is not None else ""))
    out_rows = all_trades
    path = HERE / "bb_backtest_trades.csv"
    keys = ["variant", "mode", "signal", "entry", "expiration", "entry_price", "short", "long", "width", "otm_pct", "credit",
            "breached_short", "min_close_vs_short", "taken", "exit", "exit_reason", "exit_price", "pnl", "days",
            "poc", "lower"]
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        w.writerows(out_rows)
    print(f"\nwrote {path.name}")


def rules_load_dates(path):
    out = []
    for line in Path(path).read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(date.fromisoformat(line))
    return sorted(out)


if __name__ == "__main__":
    main()
