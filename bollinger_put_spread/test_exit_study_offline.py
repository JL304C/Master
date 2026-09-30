"""
Offline test of bb_exit_study.py: synthetic bars + the fake Black-Scholes quote source from
test_real_options_offline.py. Checks that the "as tested" rule reproduces bb_real_options'
own P&L trade for trade, and that the other rules behave as described.
    python test_exit_study_offline.py
"""
import math
import random
from datetime import date, timedelta

import bb_rules as rules
import bb_real_options as ro
import bb_exit_study as es

failures = 0


def check(name, cond, detail=""):
    global failures
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        failures += 1


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

collected = []
fq = FakeQuotes()
for mode in ("signal", "baseline"):
    ro.replay("TST", bars, raw, earn, mode, fq, date(2016, 1, 1), lambda m: None,
              on_window=lambda t, ctx: collected.append((t, ctx)))
closed = [(t, c) for t, c in collected if t.get("status") == "closed"]
check("trades collected through the hook", len(closed) > 20, str(len(closed)))

diffs = [abs(es.score(c, es.day_rows(c, None), es.EXITS["as tested"])[0] - t["pnl"]) for t, c in closed
         if t["exit_quoted"] == "yes"]
check("'as tested' reproduces bb_real_options P&L trade for trade", diffs and max(diffs) < 0.02,
      f"{len(diffs)} trades, max diff {max(diffs):.4f}")

ok_tp = True
for t, c in closed:
    pnl, why = es.score(c, es.day_rows(c, None), es.EXITS["resting TP (bot)"])
    if why == "take_profit":
        ok_tp &= abs(pnl - ((c["credit"] - round(0.5 * c["credit"], 2)) * 100 - es.FEES_ROUND_TRIP)) < 1e-6
check("resting TP fills exactly at 50% of the credit", ok_tp)

reasons = set()
for t, c in closed:
    r = es.score(c, es.day_rows(c, None), es.EXITS["no stops"])
    if r:
        reasons.add(r[1])
check("'no stops' never exits on a stop", reasons <= {"take_profit", "time"}, str(reasons))


def extension(c):
    b, last = c["bars"], c["end"]
    while last + 1 < len(b) and b[last + 1]["d"] <= c["exp"]:
        last += 1
    return fq.window([c["sym_s"], c["sym_l"]], b[c["end"] + 1]["d"], b[last]["d"])


ok_exp, n_exp, n_7 = True, 0, 0
for t, c in closed:
    if not (c["end"] + 1 < len(c["bars"]) and c["bars"][c["end"] + 1]["d"] <= c["exp"]):
        continue
    ext = extension(c)
    rows = es.day_rows(c, ext)
    r = es.score(c, rows, es.EXITS["no stops, expiry"])
    if r and r[1] == "expiry":
        n_exp += 1
        close = rows[-1][2]
        settle = min(c["short"] - c["long"], max(0.0, c["short"] - close) - max(0.0, c["long"] - close))
        ok_exp &= abs(r[0] - ((c["credit"] - settle) * 100 - es.FEES_OPEN_ONLY)) < 1e-6 and rows[-1][5]
    r7 = es.score(c, rows, es.EXITS["no stops, 7 DTE"])
    if r7 and r7[1] == "time":
        n_7 += 1
        ok_exp &= any(dte <= 7 for _, dte, *_ in rows)
check("hold to expiry settles at intrinsic on the last trading day", ok_exp and n_exp > 0, f"{n_exp} settled")
check("7-DTE rule exits on the post-21-DTE days", n_7 > 0, f"{n_7} time exits at 7 DTE")
check("without an extension, long-hold rules return no result", all(
    es.score(c, es.day_rows(c, None), es.EXITS["no stops, expiry"]) is None or
    es.score(c, es.day_rows(c, None), es.EXITS["no stops, expiry"])[1] == "take_profit" for t, c in closed))

print(f"\n{failures} failure(s)")
raise SystemExit(1 if failures else 0)
