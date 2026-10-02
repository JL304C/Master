"""
Offline checks of the calendar rules and the full backtest pipeline, with no API keys:
a synthetic SPY path and Black-Scholes option quotes stand in for Alpaca and Databento.
Run: python test_backtest_offline.py
"""
from __future__ import annotations

import math
import random
import tempfile
from datetime import date, datetime, time, timedelta
from pathlib import Path

import calendar_rules as R
import spy_calendar_backtest as B

HOLIDAYS = {date(2025, 4, 18), date(2025, 5, 26), date(2025, 7, 4), date(2025, 9, 1)}


def tdays(a, b):
    d, out = a, []
    while d <= b:
        if d.weekday() < 5 and d not in HOLIDAYS:
            out.append(d)
        d += timedelta(days=1)
    return out


# --------------------------------------------------------------------------- #
# rules
# --------------------------------------------------------------------------- #
def test_calendar():
    days = tdays(date(2025, 1, 1), date(2025, 12, 31))
    is_td = set(days).__contains__
    e = date(2025, 3, 4)                                           # a Tuesday
    assert R.expiries(e, R.STRUCTURES["FF"], is_td) == (date(2025, 3, 14), date(2025, 3, 21))
    assert R.expiries(e, R.STRUCTURES["FM"], is_td) == (date(2025, 3, 14), date(2025, 3, 17))
    assert R.expiries(e, R.STRUCTURES["WF"], is_td) == (date(2025, 3, 12), date(2025, 3, 14))
    assert R.time_stop_day(e, date(2025, 3, 14), R.STRUCTURES["FF"], days) == date(2025, 3, 11)   # next Tuesday
    assert R.time_stop_day(e, date(2025, 3, 12), R.STRUCTURES["WF"], days) == date(2025, 3, 10)   # Monday
    # Good Friday: the weekly expires Thursday
    assert R.expiries(date(2025, 4, 8), R.STRUCTURES["FF"], is_td) == (date(2025, 4, 17), date(2025, 4, 25))
    # Memorial Day Monday long leg -> no FM trade that week
    assert R.expiries(date(2025, 5, 13), R.STRUCTURES["FM"], is_td) is None
    # Tuesday after a holiday Monday is still a Tuesday entry; a closed Tuesday moves to Wednesday
    ed = R.entry_days(days)
    assert date(2025, 5, 27) in ed and len(ed) == len({d.isocalendar()[:2] for d in days})
    days2 = [d for d in days if d != date(2025, 3, 4)]
    assert date(2025, 3, 5) in R.entry_days(days2)
    assert R.fomc_in_window(e, date(2025, 3, 11), [date(2025, 3, 19)]) is False
    assert R.fomc_in_window(e, date(2025, 3, 11), [date(2025, 3, 5)]) is True
    assert R.pick_strike(589.4, [585, 589, 590, 595], below=600) == 589
    assert R.pick_strike(612, [600, 605, 610], above=600) == 610
    assert R.pick_strike(612, [595, 600], above=600) is None
    print("calendar + strikes ok")


def m(i, val, hi=600, lo=590):
    return dict(ts=datetime(2025, 3, 4, 10) + timedelta(minutes=i), hi=hi, lo=lo, val=val)


def test_simulate():
    put_k, call_k, debit = 580, 610, 2.00
    # scale out: half at +20% (2.40), half at +30% (2.60)
    f = R.simulate(debit, [m(0, 2.1), m(1, 2.45), m(2, 2.5), m(3, 2.7), m(4, 2.0)], put_k, call_k, R.SPEC_EXITS)
    assert [(x[0], round(x[1], 2), x[2]) for x in f] == [(0.5, 2.4, "target 20%"), (0.5, 2.6, "target 30%")]
    assert R.pnl(f, debit, 0) == 50.0
    # gap through both targets in one minute -> both fill at their limits
    f = R.simulate(debit, [m(0, 2.1), m(1, 3.0)], put_k, call_k, R.SPEC_EXITS)
    assert len(f) == 2 and R.pnl(f, debit, 0) == 50.0
    # touch the call strike -> out at the NEXT minute's value
    f = R.simulate(debit, [m(0, 2.1), m(1, 1.9, hi=610.2), m(2, 1.6), m(3, 2.9)], put_k, call_k, R.SPEC_EXITS)
    assert f == [(1.0, 1.6, "touch", m(2, 0)["ts"])] and R.pnl(f, debit, 0.24) == -40.24
    # half out at +20%, then a touch takes the rest
    f = R.simulate(debit, [m(0, 2.45), m(1, 2.2, lo=579), m(2, 2.0)], put_k, call_k, R.SPEC_EXITS)
    assert R.final_reason(f) == "target 20% + touch" and R.pnl(f, debit, 0) == 20.0
    # underwater between the strikes -> time stop at the last minute's value
    f = R.simulate(debit, [m(0, 1.8), m(1, None), m(2, 1.5)], put_k, call_k, R.SPEC_EXITS)
    assert f[-1][1:3] == (1.5, "time stop")
    # missing quote on the last minute -> last known value
    f = R.simulate(debit, [m(0, 1.8), m(1, None)], put_k, call_k, R.SPEC_EXITS)
    assert f[-1][1] == 1.8
    # touch ignored when the touch stop is off
    f = R.simulate(debit, [m(0, 2.1, hi=700), m(1, 1.0)], put_k, call_k, R.EXIT_GRID[-1])
    assert R.final_reason(f) == "time stop"
    assert R.simulate(debit, [m(0, None)], put_k, call_k, R.SPEC_EXITS) is None
    # VIX hold: exit at the end of a day whose VIX close is below the entry VIX, never on the last minute
    vh = R.ExitRules("vix", targets=(), touch_stop=False, vix_hold=True)
    p = [m(0, 2.1), dict(m(1, 2.05), vix=18.0), m(2, 2.3), dict(m(3, 1.9), vix=16.0), m(4, 2.6)]
    f = R.simulate(debit, p, put_k, call_k, vh, vix_entry=17.0)
    assert f == [(1.0, 1.9, "VIX fell", p[3]["ts"])], f
    assert R.final_reason(R.simulate(debit, p, put_k, call_k, vh, vix_entry=15.0)) == "time stop"
    assert R.final_reason(R.simulate(debit, p[:4], put_k, call_k, vh, vix_entry=17.0)) == "time stop"
    print("exit simulation ok")


# --------------------------------------------------------------------------- #
# full pipeline on synthetic data
# --------------------------------------------------------------------------- #
def ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs(S, K, T, iv, right):
    if T <= 0:
        return max(0.0, S - K) if right == "C" else max(0.0, K - S)
    d1 = (math.log(S / K) + 0.5 * iv * iv * T) / (iv * math.sqrt(T))
    d2 = d1 - iv * math.sqrt(T)
    return S * ncdf(d1) - K * ncdf(d2) if right == "C" else K * ncdf(-d2) - S * ncdf(-d1)


class FakeData:
    """Same interface as B.MarketData. SPY follows a seeded random walk at 1-minute resolution;
    VIX switches regime so both VIX < 20 and >= 20 weeks occur; quotes are Black-Scholes with a
    slightly upward term structure, $0.01-0.05 wide."""
    offline = True

    def __init__(self, a, b, seed=7):
        self.days = tdays(a, b)
        rng = random.Random(seed)
        self.px, self.vixd, S = {}, {}, 580.0
        for i, d in enumerate(self.days):
            v = 15.0 if (i // 40) % 2 == 0 else 26.0
            self.vixd[d] = v + rng.uniform(-1, 1)
            sig = v / 100 / math.sqrt(252 * 390)
            for k in range(390):
                S *= math.exp(rng.gauss(0, sig))
                self.px[datetime.combine(d, time(9, 30)) + timedelta(minutes=k)] = S
        self.calls = 0

    def trading_days(self, a, b):
        return [d for d in self.days if a <= d <= b]

    def minutes(self, d):
        out = {}
        for k in range(390):
            t = datetime.combine(d, time(9, 30)) + timedelta(minutes=k)
            if t in self.px:
                p = self.px[t]
                out[t] = (p * 1.0003, p * 0.9997, p)
        return out

    def vix(self):
        return dict(self.vixd)

    def index(self, name="VIX"):
        k = {"VIX": 1.0, "VIX9D": None, "VIX3M": 1.06}[name]
        if k is None:                                              # VIX9D above VIX in the high-VIX regime
            return {d: v * (1.08 if v > 20 else 0.92) for d, v in self.vixd.items()}
        return {d: v * k for d, v in self.vixd.items()}

    def is_cached(self, *a):
        return True

    def cost(self, *a):
        return 0.0

    def quotes(self, symbols, start, end):
        self.calls += 1
        out = {}
        step = timedelta(minutes=5)
        for sym in symbols:
            exp = datetime.strptime(sym[6:12], "%y%m%d").date()
            right, K = sym[12], int(sym[13:]) / 1000
            if exp not in set(self.days):
                continue                                            # not listed
            t = start + (step - timedelta(minutes=start.minute % 5)) if start.minute % 5 else start
            while t <= end:
                S = self.px.get(t - timedelta(minutes=1))
                if S is not None:
                    T = (datetime.combine(exp, time(16)) - t).total_seconds() / (365 * 86400)
                    iv = self.vixd[t.date()] / 100 * (1 + 0.02 * T * 52)
                    p = bs(S, K, T, iv, right)
                    half = max(0.01, min(0.05, p * 0.01))
                    out.setdefault(sym, []).append((t, round(max(0.01, p - half), 2), round(p + half, 2)))
                t += step
        return out


def ff_debit(by, d):
    return [t for t in by["FF"] if t["entry"] == d][0]["debit_mid"]


def test_pipeline():
    data = FakeData(date(2025, 1, 2), date(2025, 9, 30))
    old = B.load_fomc
    B.load_fomc = lambda: [date(2025, 3, 19), date(2025, 5, 7), date(2025, 6, 18), date(2025, 7, 30)]
    lines = []
    try:
        trades, skips = B.replay(data, date(2025, 1, 7), date(2025, 8, 31), ["FF", "FM", "WF", "FF2"], 1.0,
                                 log=lambda s="": lines.append(s))
    finally:
        B.load_fomc = old
    by = {}
    for t in trades:
        by.setdefault(t["structure"], []).append(t)
    assert all(len(by.get(s, [])) >= 25 for s in ("FF", "FM", "WF", "FF2")), {k: len(v) for k, v in by.items()}
    f2 = [t for t in by["FF2"] if t["entry"] == date(2025, 3, 4)][0]
    assert f2["short_exp"] == date(2025, 3, 14) and f2["long_exp"] == date(2025, 3, 28)
    assert f2["debit_mid"] > ff_debit(by, date(2025, 3, 4))                    # more time bought -> bigger debit
    for t in trades:
        assert t["curve"] and all(isinstance(x, float) and isinstance(tc, bool) for x, tc in t["curve"])
        assert t["vix9d"] and t["vix3m"] and t["vix_pct60"] is not None or t["entry"] < date(2025, 2, 15)
    assert any(t["grid"]["spec + exit when VIX closes below entry"] != t["grid"][R.SPEC_EXITS.name] for t in by["FF"])
    for t in trades:
        assert t["put_k"] < t["spot"] < t["call_k"]
        assert t["entry"] < t["time_stop"] < t["short_exp"] < t["long_exp"]
        assert t["debit_nat"] >= t["debit_mid"] > 0
        if t["exit nat/nat"] == t["exit mid/nat"] in ("time stop", "touch") and t["exit_ts nat/nat"] == t["exit_ts mid/nat"]:
            assert t["pnl nat/nat"] <= t["pnl mid/nat"] + 1e-9           # same exit, higher entry price
        if t["exit mid/mid"] == t["exit mid/nat"] in ("time stop", "touch") and t["exit_ts mid/mid"] == t["exit_ts mid/nat"]:
            assert t["pnl mid/nat"] <= t["pnl mid/mid"] + 1e-9           # same exit, worse exit price
        assert -t["debit_nat"] * 100 - B.FEES - 1e-6 <= t["pnl nat/nat"]
        assert 0.005 < t["em_pct"] < 0.08, t["em_pct"]
    ff = [t for t in by["FF"] if t["entry"] == date(2025, 3, 4)]
    assert ff and ff[0]["short_exp"] == date(2025, 3, 14) and ff[0]["long_exp"] == date(2025, 3, 21)
    assert ff[0]["time_stop"] == date(2025, 3, 11) and ff[0]["fomc"] is False
    assert any(t["fomc"] for t in by["FF"]) and any(not t["fomc"] for t in by["FF"])
    assert any(t["vix"] < 20 for t in by["FF"]) and any(t["vix"] >= 20 for t in by["FF"])
    reasons = {t["exit mid/nat"].split(" + ")[-1] for t in trades}
    assert {"time stop", "touch"} & reasons and any(r.startswith("target") for r in reasons), reasons
    good_friday = [t for t in by["FF"] if t["entry"] == date(2025, 4, 8)]
    assert good_friday and good_friday[0]["short_exp"] == date(2025, 4, 17)
    assert not [t for t in by["FM"] if t["entry"] == date(2025, 5, 13)]          # Memorial Day long leg

    B.report(trades, skips, ["FF", "FM", "WF", "FF2"], date(2025, 1, 7), date(2025, 8, 31), lambda s="": lines.append(s))
    text = "\n".join(lines)
    for needle in ("VIX < 20, no FOMC  <- the spec", "FF/WF switch on VIX", "Exit rules on the spec trades", "By year",
                   "His profit curve", "VIX9D > VIX (front stress)", "VIX < 0.95 x VIX3M (contango)",
                   "FF2: short Fri ~10 DTE / long two Fridays later"):
        assert needle in text, needle
    with tempfile.TemporaryDirectory() as tmp:
        B.OUT_CSV = Path(tmp) / "t.csv"
        B.write_csv(trades)
        assert len(B.OUT_CSV.read_text().splitlines()) == len(trades) + 1
    print(f"pipeline ok: {len(trades)} synthetic trades ({', '.join(f'{k} {len(v)}' for k, v in sorted(by.items()))})")
    print("\n".join(lines[-60:]))


if __name__ == "__main__":
    test_calendar()
    test_simulate()
    test_pipeline()
    print("\nall offline tests passed")
