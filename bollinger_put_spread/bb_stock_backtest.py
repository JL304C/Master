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
DTE_MIN, DTE_MAX = 45, 90
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


def realized_vol(bars, i, n=RV_DAYS):
    r = [math.log(bars[k]["c"] / bars[k - 1]["c"]) for k in range(max(1, i - n + 1), i + 1)]
    return statistics.pstdev(r) * math.sqrt(252) if len(r) > 2 else 0.4


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


def monthly_expirations(first: date, last: date, trading: set[date], last_bar: date):
    out, y, m = [], first.year, first.month
    while date(y, m, 1) <= last:
        tf = rules.third_friday(y, m)
        if tf <= last_bar and tf not in trading:          # holiday Friday -> Thursday
            tf -= timedelta(days=1)
        out.append(tf)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


# --------------------------------------------------------------------------- #
# simulation
# --------------------------------------------------------------------------- #
def simulate(bars, earnings, exps, mode, iv_factor, fixed_otm=None):
    """mode 'signal': enter after a lower-band cross. mode 'baseline': enter whenever flat,
    short strike fixed_otm below the close (same expiration/width/exit rules)."""
    closes = [b["c"] for b in bars]
    trades, skips, busy_until = [], {}, -1
    start = max(rules.BB_PERIOD + 1, rules.POC_LOOKBACK)
    for i in range(start, len(bars) - 1):
        if mode == "signal":
            hit = rules.cross_below_lower(bars, i)
            if not hit:
                continue
            if i < busy_until:
                skips["spread already open on the ticker"] = skips.get("spread already open on the ticker", 0) + 1
                continue
            poc = rules.volume_profile_poc(bars[:i + 1])
            ceiling_poc, ceiling_band = poc, hit["lower"]
        else:
            if i < busy_until:
                continue
            ceiling_poc = ceiling_band = closes[i] * (1 - fixed_otm)
            poc = hit = None
        e = i + 1                                          # entry the next morning
        ed = bars[e]["d"]
        nxt = next((x for x in earnings if x >= ed), None)
        cands = [x for x in exps if DTE_MIN <= (x - ed).days <= DTE_MAX and (nxt is None or x < nxt)]
        if not cands:
            skips["no monthly before earnings"] = skips.get("no monthly before earnings", 0) + 1
            if mode == "baseline":
                busy_until = e
            continue
        exp = cands[0]
        ceiling = min(ceiling_poc, ceiling_band)
        picked = rules.pick_strikes(strike_grid(ceiling), ceiling_poc, ceiling_band, rules.target_width(closes[i]))
        if not picked:
            skips["no strikes"] = skips.get("no strikes", 0) + 1
            continue
        short, long = picked
        atm = realized_vol(bars, i) * iv_factor
        S0 = bars[e]["o"]
        credit = round(spread_value(S0, short, long, (exp - ed).days / 365, atm), 2)
        otm = 1 - short / S0
        t = dict(mode=mode, signal=bars[i]["d"], entry=ed, expiration=exp, entry_price=S0, short=short, long=long,
                 width=short - long, otm_pct=otm, credit=credit, poc=poc, lower=hit["lower"] if hit else None)
        # stock-side (no option prices): first close below the short before the time stop
        breach = None
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
            val = spread_value(closes[d], short, long, max(dte, 0) / 365, realized_vol(bars, d) * iv_factor)
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


def summarize(label, trades, skips, crosses=None):
    taken = [t for t in trades if t.get("taken") and t.get("pnl") is not None]
    considered = [t for t in trades]
    lines = [f"\n=== {label} ==="]
    if crosses is not None:
        lines.append(f"lower-band crosses: {crosses}")
    lines.append(f"expiration/strikes found: {len(considered)}   skipped: "
                 + (", ".join(f"{k} {v}" for k, v in skips.items()) or "none"))
    if considered:
        br = sum(t["breached_short"] for t in considered)
        otm = [t["otm_pct"] for t in considered]
        lines.append(f"[stock-side] short strike below the entry price: median {statistics.median(otm):.1%} "
                     f"(range {min(otm):.1%} to {max(otm):.1%})")
        lines.append(f"[stock-side] closed below the short strike before 21 DTE: {br}/{len(considered)} "
                     f"({br / len(considered):.0%})")
    low = [t for t in considered if not t.get("taken")]
    lines.append(f"[model] skipped for credit < ${MIN_CREDIT:.2f}: {len(low)}")
    if taken:
        pnl = [t["pnl"] for t in taken]
        eq = peak = dd = 0.0
        for p in pnl:
            eq += p
            peak = max(peak, eq)
            dd = min(dd, eq - peak)
        wins = sum(p > 0 for p in pnl)
        reasons = {}
        for t in taken:
            reasons[t["exit_reason"]] = reasons.get(t["exit_reason"], 0) + 1
        sd = statistics.pstdev(pnl) if len(pnl) > 1 else 0
        tstat = (statistics.mean(pnl) / (sd / math.sqrt(len(pnl)))) if sd else 0
        lines.append(f"[model] trades {len(pnl)}, won {wins} ({wins / len(pnl):.0%}), total ${sum(pnl):,.0f}, "
                     f"avg ${statistics.mean(pnl):,.0f}, worst ${min(pnl):,.0f}, max drawdown ${dd:,.0f}, t={tstat:.1f}")
        lines.append(f"[model] avg credit ${statistics.mean(t['credit'] for t in taken):.2f}, "
                     f"avg width ${statistics.mean(t['width'] for t in taken):.2f}, "
                     f"avg days held {statistics.mean(t['days'] for t in taken):.0f}")
        lines.append("[model] exits: " + ", ".join(f"{k} {v}" for k, v in sorted(reasons.items())))
        years = {}
        for t in taken:
            years[t["entry"].year] = years.get(t["entry"].year, 0) + t["pnl"]
        lines.append("[model] by year: " + ", ".join(f"{y} ${v:,.0f}" for y, v in sorted(years.items())))
    return "\n".join(lines)


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
    exps = monthly_expirations(bars[0]["d"], bars[-1]["d"] + timedelta(days=120), trading, bars[-1]["d"])
    print(f"{symbol}: {len(bars)} daily bars {bars[0]['d']} .. {bars[-1]['d']}, "
          f"{sum(1 for e in earnings if bars[0]['d'] <= e <= bars[-1]['d'] + timedelta(days=120))} earnings dates in range")

    crosses = sum(1 for i in range(max(rules.BB_PERIOD + 1, rules.POC_LOOKBACK), len(bars) - 1)
                  if rules.cross_below_lower(bars, i))
    out_rows, report = [], []
    for f in IV_FACTORS:
        sig_trades, sig_skips = simulate(bars, earnings, exps, "signal", f)
        otm = statistics.median([t["otm_pct"] for t in sig_trades]) if sig_trades else 0.10
        base_trades, base_skips = simulate(bars, earnings, exps, "baseline", f, fixed_otm=otm)
        if f == 1.0:
            report.append(summarize(f"SIGNAL strategy (IV = realized x {f})", sig_trades, sig_skips, crosses))
            report.append(summarize(f"NO-SIGNAL baseline, short {otm:.1%} below the close (IV x {f})",
                                    base_trades, base_skips))
            out_rows = sig_trades + base_trades
        else:
            taken = [t["pnl"] for t in sig_trades if t.get("pnl") is not None]
            btaken = [t["pnl"] for t in base_trades if t.get("pnl") is not None]
            report.append(f"\n[sensitivity IV x {f}] signal: {len(taken)} trades ${sum(taken):,.0f} | "
                          f"baseline: {len(btaken)} trades ${sum(btaken):,.0f}")
    print("\n".join(report))
    print("\nSignal trades (IV x 1.0):")
    for t in out_rows:
        if t["mode"] != "signal":
            continue
        print(f"  {t['signal']} -> {t['entry']} exp {t['expiration']}  {t['short']:g}/{t['long']:g}P "
              f"({t['otm_pct']:.1%} OTM)  credit~{t['credit']:.2f}  "
              f"{'BREACHED' if t['breached_short'] else 'held   '}  {t['exit_reason']}"
              + (f"  ${t['pnl']:,.0f}" if t.get("pnl") is not None else ""))
    path = HERE / "bb_backtest_trades.csv"
    keys = ["mode", "signal", "entry", "expiration", "entry_price", "short", "long", "width", "otm_pct", "credit",
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
