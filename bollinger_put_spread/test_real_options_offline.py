"""
Offline test of bb_real_options.py: synthetic daily bars + a fake quote source (Black-Scholes
bid/ask on a monthly chain) instead of Alpaca/Databento, plus the Databento dataframe parsers
on frames shaped like databento's to_df(). No network, no keys:
    python test_real_options_offline.py
"""
import math
import random
from datetime import date, timedelta

import pandas as pd

import bb_rules as rules
import bb_real_options as ro

failures = 0


def check(name, cond, detail=""):
    global failures
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        failures += 1


# --- parsers on databento-shaped frames ---------------------------------------------------
idx = pd.DatetimeIndex([pd.Timestamp("2025-03-06 14:43", tz="UTC"), pd.Timestamp("2025-03-06 14:44", tz="UTC"),
                        pd.Timestamp("2025-03-06 14:44", tz="UTC"), pd.Timestamp("2025-03-06 14:44", tz="UTC")],
                       name="ts_recv")
df = pd.DataFrame({"symbol": ["AMD   250417P00095000", "AMD   250417P00095000", "AMD   250417C00095000",
                              "AMD   250417P00092500"],
                   "bid_px_00": [0.90, 1.00, 5.0, 0.0], "ask_px_00": [1.10, 1.20, 5.2, 0.5]}, index=idx)
ch = ro.parse_chain_df(df)
check("chain: last quote per put, calls and one-sided quotes dropped",
      ch == {(date(2025, 4, 17), 95.0): (1.00, 1.20, "AMD   250417P00095000")}, str(ch))

# two days; 15:45 ET = 19:45 UTC (EST) / 20:45 UTC ... use March 2025 (EDT from Mar 9): Mar 6 is EST -> 20:45 UTC
idx = pd.DatetimeIndex([pd.Timestamp(x, tz="UTC") for x in
                        ["2025-03-06 20:40", "2025-03-06 20:45", "2025-03-06 20:50",     # 15:40, 15:45, 15:50 ET
                         "2025-11-28 17:55"]], name="ts_recv")                          # 12:55 ET, early close
df = pd.DataFrame({"symbol": ["S"] * 4, "bid_px_00": [1.0, 1.1, 9.0, 2.0], "ask_px_00": [1.2, 1.3, 9.9, 2.2]},
                  index=idx)
w = ro.parse_window_df(df)
check("window: last quote at/before 3:45 PM ET, later ones ignored", w[date(2025, 3, 6)]["S"] == (1.1, 1.3), str(w))
check("window: early-close day uses its last quote", w[date(2025, 11, 28)]["S"] == (2.0, 2.2))
check("osi parse", ro.parse_osi("SPY   250411P00520000") == (date(2025, 4, 11), "P", 520.0))

# --- replay against a fake quote source -----------------------------------------------------
random.seed(3)
bars, raw = [], {}
d, p = date(2016, 1, 4), 50.0
while d <= date(2024, 12, 31):
    if d.weekday() < 5:
        r = random.gauss(0.0006, 0.022)
        o = p
        p *= math.exp(r)
        b = dict(d=d, o=o, h=max(o, p) * 1.01, l=min(o, p) * 0.99, c=p, v=random.randint(5, 9) * 1e6)
        bars.append(b)
        split = 4 if d < date(2020, 8, 31) else 1          # a 4:1 split: raw prices 4x before it
        raw[d] = dict(b, o=o * split, c=p * split)
    d += timedelta(days=1)
by_date = {b["d"]: b for b in bars}


def bs_put(S, K, T, sig=0.40):
    T = max(T, 1 / 365)
    d1 = (math.log(S / K) + 0.5 * sig * sig * T) / (sig * math.sqrt(T))
    n = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))
    return K * n(-(d1 - sig * math.sqrt(T))) - S * n(-d1)


class FakeQuotes:
    def __init__(self):
        self.chains, self.windows = [], []

    def quote(self, S, K, exp, d):
        px = bs_put(S, K, (exp - d).days / 365, 0.40 * (1 + 0.5 * max(0, math.log(S / K))))
        return max(0.01, px - 0.05), px + 0.05

    def chain(self, ticker, d):
        self.chains.append(d)
        S = raw[d]["o"]
        out = {}
        for m in range(1, 5):
            y, mo = d.year + (d.month - 1 + m) // 12, (d.month - 1 + m) % 12 + 1
            exp = rules.third_friday(y, mo)
            step = 1.0 if S < 25 else 2.5 if S < 100 else 5.0
            k = step
            while k < S * 1.1:
                bid, ask = self.quote(S, k, exp, d)
                out[(exp, k)] = (bid, ask, f"{ticker:<6}{exp:%y%m%d}P{int(k * 1000):08d}")
                k += step
        return out

    def window(self, symbols, d0, d1):
        self.windows.append((tuple(symbols), d0, d1))
        out = {}
        for dd in sorted(x for x in raw if d0 <= x <= d1):
            out[dd] = {}
            for s in symbols:
                exp, _, k = ro.parse_osi(s)
                out[dd][s] = self.quote(raw[dd]["c"], k, exp, dd)
        return out


earn = [date(y, m, 25) for y in range(2016, 2025) for m in (1, 4, 7, 10)]
logs = []
for mode in ("signal", "baseline"):
    fq = FakeQuotes()
    trades, skips = ro.replay("TST", bars, raw, earn, mode, fq, date(2016, 1, 1), logs.append)
    closed = [t for t in trades if t.get("status") == "closed"]
    check(f"{mode}: trades replayed", len(closed) > 3, f"{len(closed)} closed, skips {skips}")
    ok = True
    for t in closed:
        nxt = next((x for x in earn if x >= t["entry"]), date.max)
        ok &= t["expiration"] == rules.third_friday(t["expiration"].year, t["expiration"].month)
        ok &= 45 <= (t["expiration"] - t["entry"]).days <= 90 and t["expiration"] < nxt
        ok &= t["credit"] >= 0.50 and t["short"] > t["long"]
        ok &= t["exit_natural"] >= t["exit_mid"] - 1e-9 and t["pnl"] <= t["pnl_mid"] + 1e-9
        ok &= t["exit_reason"] in ("stop_loss", "backup_stop", "take_profit", "time_stop")
        ok &= (t["expiration"] - t["exit"]).days >= 0
    check(f"{mode}: monthly 45-90 DTE before earnings, credit >= 0.50, natural exit >= mid", ok)
    spans = sorted((t["entry"], t["exit"]) for t in closed)
    check(f"{mode}: never two spreads open at once", all(a[1] <= b[0] for a, b in zip(spans, spans[1:])))
    if mode == "signal":
        sig_ok = all(rules.cross_above_lower(bars, next(i for i, b in enumerate(bars) if b["d"] == t["signal"]))
                     for t in closed)
        check("signal: every trade follows a close back above the lower band", sig_ok)
        pre = [t for t in closed if t["entry"] < date(2020, 8, 31)]
        check("signal: strikes in raw (pre-split, 4x) prices", all(t["short"] < t["price"] * 1.2 and
                                                                  t["short"] > t["price"] * 0.5 for t in pre),
              f"{[(t['price'], t['short']) for t in pre[:3]]}")
        check("signal: POC/band caps respected (x 0.95)", all(t["short"] < min(t["poc"], t["lower"]) * 0.95 + 1e-9
                                                            for t in closed))
    else:
        check("baseline: entries only after the last trading day of a week",
              all(ro.last_day_of_week(bars, next(i for i, b in enumerate(bars) if b["d"] == t["signal"]))
                  for t in closed))
    check(f"{mode}: no chain bought for a morning the earnings rule already blocks",
          all(ro.monthly_possible(dd, next((x for x in earn if x >= dd), None)) for dd in fq.chains))
    check(f"{mode}: one quote window per trade", len(fq.windows) == len([t for t in trades if t.get("status")]))
    straddle = [t for t in trades if t.get("status") and t["entry"] < date(2020, 8, 31) <= (t.get("exit") or t["entry"])]
    check(f"{mode}: no trade held across the split", not straddle, str(straddle[:1]))

check("META options are FB before 2022-06-09", ro.option_root("META", date(2021, 1, 4)) == "FB"
      and ro.option_root("META", date(2022, 6, 9)) == "META" and ro.option_root("AMD", date(2016, 1, 4)) == "AMD")
check("a META trade across the rename is detected", ro.root_changes_between("META", date(2022, 5, 20), date(2022, 6, 24))
      and not ro.root_changes_between("META", date(2022, 6, 10), date(2022, 7, 22)))

print(ro.row("POOLED", closed))
print(f"\n{failures} failure(s)")
raise SystemExit(1 if failures else 0)
