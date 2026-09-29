"""
Offline test of bb_rules.py and bb_put_spread_bot.py: replaces the Alpaca clients and
Alpha Vantage with fakes (synthetic AMD daily bars, a Black-Scholes option chain) and
walks each decision path, including that every order request passes alpaca-py's own
validation. No network, no keys:  python test_bot_offline.py
"""
import io, json, math, os, sys, tempfile, types
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
os.environ.setdefault("ALPHAVANTAGE_API_KEY", "test")
sys.argv = [sys.argv[0]]
import bb_rules as rules
import bb_put_spread_bot as bot
from alpaca.trading.enums import PositionIntent, TimeInForce

failures = 0


def check(name, cond, detail=""):
    global failures
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        failures += 1


# --------------------------------------------------------------------------- #
# pure rules
# --------------------------------------------------------------------------- #
closes = [100.0] * 49 + [110.0]
m, lo, hi = rules.bollinger(closes, 49)
sd = math.sqrt((49 * (m - 100) ** 2 + (110 - m) ** 2) / 50)
check("bollinger: population sd, 2 sd bands", abs(m - 100.2) < 1e-9 and abs(lo - (m - 2 * sd)) < 1e-9)

flat = [dict(d=None, o=100, h=101, l=99, c=100 + (0.5 if i % 2 else -0.5), v=1e6) for i in range(60)]
check("cross: no signal on a flat series", rules.cross_below_lower(flat) is None)
drop = flat + [dict(d=None, o=100, h=100, l=90, c=91, v=1e6)]
check("cross: fires on the first close below the lower band", rules.cross_below_lower(drop) is not None)
drop2 = drop + [dict(d=None, o=91, h=91, l=85, c=86, v=1e6)]
check("cross: no repeat signal while already below", rules.cross_below_lower(drop2) is None)

prof = [dict(l=10, h=20, v=100)] * 5 + [dict(l=14, h=15, v=1000)]
poc = rules.volume_profile_poc(prof, lookback=6, nbins=10)
check("POC lands in the heavy 14-15 bin", 14 <= poc <= 15, f"{poc:.2f}")

listed = [100, 105, 110, 115, 120]
check("strikes: highest below both POC and band", rules.pick_strikes(listed, 118, 112, 5) == (110, 105))
check("strikes: strictly below (a strike AT the band is not taken)", rules.pick_strikes(listed, 130, 115, 5) == (110, 105))
check("strikes: nothing below the short -> None", rules.pick_strikes([110, 120], 118, 112, 5) is None)
check("width: 1% of price", abs(rules.target_width(607.87) - 6.0787) < 1e-9)
amd = [370.0, 380.0, 390.0, 400.0, 410.0]         # AMD Nov-20 puts from the real dry run
check("width: $10-step chain -> next strike down (AMD 400/390)",
      rules.pick_strikes(amd, 511.85, 407.41, rules.target_width(607.87)) == (400, 390))
fine = [390, 392.5, 395, 397.5, 400, 402.5, 405, 407.5, 410]
check("width: $2.50-step chain -> strike nearest short-6.08 (405/400)",
      rules.pick_strikes(fine, 511.85, 407.41, 6.08) == (405, 400))
check("width: tie goes to the narrower spread", rules.pick_strikes([90, 95, 100, 110], 200, 111, 7.5) == (110, 100))
check("width: scales up on a pricier stock", rules.pick_strikes(list(range(800, 1001, 5)), 2000, 951, 20) == (950, 930))

check("3rd Friday Nov 2026 = Nov 20", rules.third_friday(2026, 11) == date(2026, 11, 20))
# Good Friday 2027-04-16 is the 3rd Friday -> Thursday 04-15 is the monthly
check("holiday monthly: Thursday counts when the Friday isn't listed",
      rules.is_standard_monthly(date(2027, 4, 15), {date(2027, 4, 15)}))
t0 = date(2026, 9, 28)
lst = [date(2026, 11, 13), date(2026, 11, 20), date(2026, 12, 18), date(2027, 1, 15)]
check("expirations: monthlies in 45-90 DTE", rules.eligible_expirations(lst, t0, None) == [date(2026, 11, 20), date(2026, 12, 18)])
check("expirations: none after earnings", rules.eligible_expirations(lst, t0, date(2026, 12, 1)) == [date(2026, 11, 20)])
check("expirations: earnings before the first -> none", rules.eligible_expirations(lst, t0, date(2026, 11, 3)) == [])

check("exit: take profit at <= 50%", rules.exit_reason(1.0, 0.50, 100, 90, 40) == "take_profit")
check("exit: stop at >= 2x", rules.exit_reason(1.0, 2.0, 100, 90, 40) == "stop_loss")
check("exit: backup stop under short strike", rules.exit_reason(1.0, 1.2, 89.9, 90, 40) == "backup_stop")
check("exit: time stop at 21 DTE", rules.exit_reason(1.0, 0.9, 100, 90, 21) == "time_stop")
check("exit: nothing otherwise", rules.exit_reason(1.0, 0.9, 100, 90, 22) is None)

bk = [dict(ticker="NVDA", sector="Technology"), dict(ticker="MSFT", sector="Technology")]
check("limits: sector cap", "sector" in rules.limit_violation("AMD", "Technology", bk, 5, 2, 1))
check("limits: ticker cap", "AMD" in rules.limit_violation("AMD", "Tech2", [dict(ticker="AMD", sector="x")], 5, 2, 1))
check("limits: total cap", "5" in rules.limit_violation("X", "Y", [dict(ticker=str(i), sector=str(i)) for i in range(5)], 5, 2, 1))
check("limits: ok", rules.limit_violation("AMD", "Technology", bk[:1], 5, 2, 1) is None)

csv_text = ('symbol,name,reportDate,fiscalDateEnding,estimate,currency\n'
            'AMD,"Advanced Micro Devices, Inc",2027-02-03,2026-12-31,1.1,USD\n'
            'AMD,"Advanced Micro Devices, Inc",2026-11-04,2026-09-30,1.0,USD\n')
check("earnings CSV: next date (quoted comma in name)", rules.parse_earnings_csv(csv_text, "AMD", t0) == (True, date(2026, 11, 4)))
check("earnings CSV: rate-limit JSON is not ok", rules.parse_earnings_csv('{"Information": "limit"}', "AMD", t0)[0] is False)

# --------------------------------------------------------------------------- #
# bot, against fakes
# --------------------------------------------------------------------------- #
TODAY = date(2026, 9, 28)          # a Monday


class FakeDate(date):
    @classmethod
    def today(cls):
        return TODAY


bot.date = FakeDate


def weekdays_back(end, n):
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


def make_bars(end, crash):
    """~200 sessions oscillating around 160 (volume heaviest near 165), ending on `end`.
    crash=True: an oversold close of 150 (under the lower band) the day before, then a close
    of 157, back above the band -- the variant-A "reclaim" signal on the last bar.
    crash="below": the last close is 150, the original rules' cross-below signal."""
    days = weekdays_back(end, 200)
    bars = []
    for i, d in enumerate(days):
        c = 160 + 4 * math.sin(i / 3)
        bars.append(dict(d=d, o=c, h=c + 1.5, l=c - 1.5, c=c, v=(3e6 if 163 < c < 166 else 1e6)))
    if crash == "below":
        bars[-1].update(c=150.0, l=149.0, o=158.0, h=158.5)
        bars[-2].update(c=160.0)
    elif crash:
        bars[-3].update(c=160.0)
        bars[-2].update(c=150.0, l=149.0, o=158.0, h=158.5)
        bars[-1].update(c=157.0, l=150.0, o=151.0, h=157.5)
    return bars


def bs_put(S, K, T, sig):
    d1 = (math.log(S / K) + 0.5 * sig * sig * T) / (sig * math.sqrt(T)); d2 = d1 - sig * math.sqrt(T)
    n = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))
    return K * n(-d2) - S * n(-d1)


def monthly(y, m):
    return rules.third_friday(y, m)


class Fake:
    def __init__(self, bars, price=None, vol=0.55):
        self.bars, self.price, self.vol = bars, price or bars[-1]["c"], vol
        self.submitted, self.orders, self.positions, self.cancelled = [], {}, [], []

    # stock data
    def get_stock_bars(self, req):
        return {req.symbol_or_symbols: [types.SimpleNamespace(
            timestamp=datetime.combine(b["d"], datetime.min.time(), timezone.utc) + timedelta(hours=4),
            open=b["o"], high=b["h"], low=b["l"], close=b["c"], volume=b["v"]) for b in self.bars]}

    def get_stock_latest_trade(self, req):
        return {req.symbol_or_symbols: types.SimpleNamespace(price=self.price)}

    # trading
    def get_calendar(self, req):
        d, out = req.start, []
        while d <= req.end:
            if d.weekday() < 5:
                out.append(types.SimpleNamespace(date=d))
            d += timedelta(days=1)
        return out

    def get_option_contracts(self, req):
        lo, hi = date.fromisoformat(req.expiration_date_gte), date.fromisoformat(req.expiration_date_lte)
        exps, d = [], lo
        while d <= hi:
            if d.weekday() == 4:
                exps.append(d)
            d += timedelta(days=1)
        out = []
        for e in exps:
            step = 5 if e == monthly(e.year, e.month) else 2.5
            k = 100.0
            while k <= 220:
                out.append(types.SimpleNamespace(expiration_date=e, strike_price=k,
                                                 symbol=f"AMD{e:%y%m%d}P{int(k * 1000):08d}"))
                k += step
        return types.SimpleNamespace(option_contracts=out, next_page_token=None)

    def get_all_positions(self):
        return [types.SimpleNamespace(symbol=s, qty=q) for s, q in self.positions]

    def get_order_by_id(self, oid):
        st, q, fap = self.orders[oid]
        return types.SimpleNamespace(status=st, filled_qty=q, filled_avg_price=fap)

    def cancel_order_by_id(self, oid):
        self.cancelled.append(oid)
        st, q, fap = self.orders[oid]
        self.orders[oid] = ("canceled" if st != "filled" else st, q, fap)

    def submit_order(self, req):
        self.submitted.append(req)
        oid = f"order-{len(self.submitted)}"
        self.orders[oid] = ("new", 0, None)
        return types.SimpleNamespace(id=oid)

    # option data
    def get_option_snapshot(self, req):
        out = {}
        for sym in req.symbol_or_symbols:
            e = datetime.strptime(sym[3:9], "%y%m%d").date(); K = int(sym[10:]) / 1000
            px = bs_put(self.price, K, max((e - TODAY).days, 1) / 365, self.vol)
            out[sym] = types.SimpleNamespace(latest_quote=types.SimpleNamespace(
                bid_price=max(0.01, px - 0.05), ask_price=px + 0.05))
        return out


EARN_CSV = 'symbol,name,reportDate,fiscalDateEnding,estimate,currency\nAMD,AMD,{d},2026-12-31,1.0,USD\n'


def install(fake, tmp, earnings="2027-02-03"):
    bot.trade_client = bot.stock_client = bot.option_client = fake
    for name in ("STATE_FILE", "LOG_JSONL", "SIGNALS_CSV", "TRADES_CSV", "EARNINGS_CACHE"):
        setattr(bot, name, Path(tmp) / getattr(bot, name).name)
    bot.DRY_RUN = False
    body = EARN_CSV.format(d=earnings) if earnings else '{"Information": "rate limit"}'
    bot.urllib.request.urlopen = lambda url, timeout=30: io.BytesIO(body.encode())


def run(mode, *extra):
    sys.argv = [sys.argv[0], mode, *extra]
    bot.DRY_RUN = "--dry-run" in sys.argv
    bot.main()


def logs():
    return [json.loads(l) for l in bot.LOG_JSONL.read_text().splitlines()]


def signals():
    import csv
    return list(csv.DictReader(bot.SIGNALS_CSV.open())) if bot.SIGNALS_CSV.exists() else []


SIGNAL_DAY = date(2026, 9, 25)     # Friday
ENTRY_DAY = date(2026, 9, 28)      # Monday


def signal_then_enter(tmp, earnings="2027-02-03", vol=0.55, crash=True, price=None):
    global TODAY
    TODAY = SIGNAL_DAY
    f = Fake(make_bars(SIGNAL_DAY, crash), price=price, vol=vol); install(f, tmp, earnings)
    run("--signal")
    TODAY = ENTRY_DAY
    run("--enter")
    return f


with tempfile.TemporaryDirectory() as tmp:
    f = signal_then_enter(tmp)
    sg = signals()
    check("B1 signal logged as pending", sg and sg[0]["outcome"] == "pending", sg[0]["reason"] if sg else "")
    check("B1 next morning: order placed", len(f.submitted) == 1 and sg[-1]["outcome"] == "order_placed", sg[-1]["reason"])
    if f.submitted:
        req = f.submitted[0]
        sk, lk = int(req.legs[0].symbol[10:]) / 1000, int(req.legs[1].symbol[10:]) / 1000
        exp = datetime.strptime(req.legs[0].symbol[3:9], "%y%m%d").date()
        st = json.loads(bot.STATE_FILE.read_text())["spreads"][0]
        sig = st["signal"]
        check("B1 mleg DAY limit, negative (credit) price, 1 contract",
              req.order_class.value == "mleg" and req.limit_price < 0 and req.qty == 1
              and req.time_in_force == TimeInForce.DAY, str(req.limit_price))
        cap = min(sig["poc"], sig["lower"]) * (1 - bot.STRIKE_OFFSET)
        step = 5 if exp == monthly(exp.year, exp.month) else 2.5
        check("B1 STO short / BTO long, next strike down (1% of 157 < one strike)",
              req.legs[0].position_intent == PositionIntent.SELL_TO_OPEN
              and req.legs[1].position_intent == PositionIntent.BUY_TO_OPEN and sk - lk == step, f"{sk}/{lk}")
        check("B1 (B) short below POC and band, both lowered 5%", sk < cap,
              f"short {sk} cap {cap:.2f} poc {sig['poc']:.2f} lower {sig['lower']:.2f}")
        check("B1 short is the highest such strike", sk + step >= cap)
        check("B1 (D) nearest expiration 30-60 DTE, weekly allowed", 30 <= (exp - ENTRY_DAY).days <= 60
              and exp.weekday() == 4 and (exp - ENTRY_DAY).days < 37, str(exp))
        check("B1 limit = mid credit >= 0.50", abs(-req.limit_price - st["limit_credit"]) < 1e-9 and st["limit_credit"] >= 0.5)

        # entry fills -> open + resting GTC take-profit at 50%
        f.orders["order-1"] = ("filled", 1, -st["limit_credit"])
        f.positions = [(req.legs[0].symbol, -1), (req.legs[1].symbol, 1)]
        run("--manage")
        st = json.loads(bot.STATE_FILE.read_text())["spreads"][0]
        check("B2 fill reconciled: open with entry credit", st["status"] == "open" and st["entry_credit"] == -f.orders["order-1"][2])
        tp = f.submitted[1] if len(f.submitted) > 1 else None
        check("B2 resting GTC buy-to-close at 50% credit", tp is not None and tp.time_in_force == TimeInForce.GTC
              and tp.limit_price > 0 and abs(tp.limit_price - round(st["entry_credit"] / 2, 2)) < 1e-9
              and tp.legs[0].position_intent == PositionIntent.BUY_TO_CLOSE)
        check("B2 no exit on day one", len(f.submitted) == 2, logs()[-2].get("exit"))

        # price falls under the short strike -> backup stop / stop: cancels TP, submits a debit close
        f.price = sk - 3
        run("--manage")
        ev = [e for e in logs() if e["action"] == "close_submitted"]
        check("B3 stop: resting TP cancelled first", "order-2" in f.cancelled)
        check("B3 stop: close submitted", ev and ev[-1]["exit_reason"] in ("stop_loss", "backup_stop"), ev[-1]["exit_reason"] if ev else "")
        cl = f.submitted[-1]
        check("B3 close is mleg debit limit BTC/STC", cl.limit_price > 0 and cl.legs[0].position_intent == PositionIntent.BUY_TO_CLOSE
              and cl.legs[1].position_intent == PositionIntent.SELL_TO_CLOSE)
        # close fills -> trade row with P&L
        f.orders["order-3"] = ("filled", 1, 2.40)
        run("--manage")
        import csv
        tr = list(csv.DictReader(bot.TRADES_CSV.open()))
        check("B4 trade logged with P&L and reason", len(tr) == 1 and abs(float(tr[0]["pnl"]) - round((st["entry_credit"] - 2.40) * 100, 2)) < 1e-6
              and tr[0]["exit_reason"] in ("stop_loss", "backup_stop"), str(tr[0] if tr else None))
        check("B4 spread no longer tracked", json.loads(bot.STATE_FILE.read_text())["spreads"] == [])

with tempfile.TemporaryDirectory() as tmp:
    f = signal_then_enter(tmp)
    st = json.loads(bot.STATE_FILE.read_text())["spreads"][0]
    f.orders["order-1"] = ("filled", 1, -st["limit_credit"])
    f.positions = [(st["short_symbol"], -1), (st["long_symbol"], 1)]
    f.price = 185                               # rally -> spread decays
    run("--manage")
    ev = [e for e in logs() if e["action"] == "close_submitted"]
    check("B5 take profit when value <= 50% credit", ev and ev[-1]["exit_reason"] == "take_profit")
with tempfile.TemporaryDirectory() as tmp:
    f = signal_then_enter(tmp)
    st = json.loads(bot.STATE_FILE.read_text())["spreads"][0]
    f.orders["order-1"] = ("filled", 1, -st["limit_credit"])
    f.positions = [(st["short_symbol"], -1), (st["long_symbol"], 1)]
    run("--manage")
    f.orders["order-2"] = ("filled", 1, round(st["limit_credit"] / 2, 2))
    run("--manage")
    import csv
    tr = list(csv.DictReader(bot.TRADES_CSV.open()))
    check("B6 resting GTC fill recorded as take profit", len(tr) == 1 and tr[0]["exit_reason"].startswith("take_profit"))
with tempfile.TemporaryDirectory() as tmp:
    f = signal_then_enter(tmp)
    st = json.loads(bot.STATE_FILE.read_text())["spreads"][0]
    f.orders["order-1"] = ("filled", 1, -st["limit_credit"])
    f.positions = [(st["short_symbol"], -1), (st["long_symbol"], 1)]
    TODAY = date.fromisoformat(st["expiration"]) - timedelta(days=21)
    run("--manage")
    ev = [e for e in logs() if e["action"] == "close_submitted"]
    check("B7 time stop at 21 DTE", ev and ev[-1]["exit_reason"] == "time_stop")

with tempfile.TemporaryDirectory() as tmp:
    f = signal_then_enter(tmp, crash=False)
    check("B8 no cross -> no signal, no order", not signals() and not f.submitted)

with tempfile.TemporaryDirectory() as tmp:
    f = signal_then_enter(tmp, earnings="2026-10-27")
    sg = signals()
    check("B9 earnings before every 30-60 DTE expiration -> skipped with reason",
          not f.submitted and sg[-1]["outcome"] == "skipped" and "earnings" in sg[-1]["reason"], sg[-1]["reason"])

with tempfile.TemporaryDirectory() as tmp:
    f = signal_then_enter(tmp, earnings=None)
    sg = signals()
    check("B10 Alpha Vantage failure -> skipped, no order", not f.submitted and sg[-1]["outcome"] == "skipped", sg[-1]["reason"])

with tempfile.TemporaryDirectory() as tmp:
    f = signal_then_enter(tmp, vol=0.10, price=175)   # overnight rebound, low vol
    sg = signals()
    check("B11 credit < $0.50 -> skipped", not f.submitted and "credit" in sg[-1]["reason"], sg[-1]["reason"])

with tempfile.TemporaryDirectory() as tmp:
    TODAY = SIGNAL_DAY
    f = Fake(make_bars(SIGNAL_DAY, True)); install(f, tmp)
    run("--signal")
    TODAY = date(2026, 9, 29)                     # skipped Monday -> Tuesday is too late
    run("--enter")
    check("B12 stale signal skipped", not f.submitted and signals()[-1]["reason"].startswith("stale"))

with tempfile.TemporaryDirectory() as tmp:
    f = signal_then_enter(tmp)
    f.orders["order-1"] = ("expired", 0, None)
    run("--manage")
    sg = signals()
    check("B13 unfilled entry -> logged, dropped", sg[-1]["outcome"] == "entry_not_filled"
          and json.loads(bot.STATE_FILE.read_text())["spreads"] == [])

with tempfile.TemporaryDirectory() as tmp:
    f = signal_then_enter(tmp)                    # AMD now open/opening
    TODAY = date(2026, 10, 2)
    f.bars = make_bars(TODAY, True)
    run("--signal")
    sg = signals()
    check("B14 second AMD signal while one is open -> skipped (1 per ticker)",
          sg[-1]["outcome"] == "skipped" and "AMD" in sg[-1]["reason"], sg[-1]["reason"])

with tempfile.TemporaryDirectory() as tmp:
    TODAY = SIGNAL_DAY
    f = Fake(make_bars(SIGNAL_DAY, True)); install(f, tmp)
    run("--signal")
    TODAY = ENTRY_DAY
    run("--enter", "--dry-run")
    check("B15 dry run: decides, submits nothing, keeps state", not f.submitted
          and json.loads(bot.STATE_FILE.read_text())["pending_signals"])
    run("--enter", "--dry-run", "--force-entry", "AMD")
    check("B16 --force-entry dry run walks the entry path", not f.submitted and signals()[-1]["outcome"] in ("order_placed", "skipped"),
          signals()[-1]["reason"])

with tempfile.TemporaryDirectory() as tmp:
    # --ignore-earnings: earnings on Nov 4 would block every expiry; the flag lets the dry run pick strikes
    TODAY = ENTRY_DAY
    f = Fake(make_bars(SIGNAL_DAY, True)); install(f, tmp, earnings="2026-11-04")
    bot.IGNORE_EARNINGS = True
    run("--enter", "--dry-run", "--force-entry", "AMD", "--ignore-earnings")
    bot.IGNORE_EARNINGS = False
    sg = signals()
    check("B17 --ignore-earnings dry run picks strikes past earnings, submits nothing",
          not f.submitted and sg[-1]["outcome"] == "order_placed" and sg[-1]["reason"].startswith("[TEST")
          and sg[-1]["short_strike"], sg[-1]["reason"])

import subprocess
r = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "bb_put_spread_bot.py"),
                    "--enter", "--ignore-earnings"], capture_output=True, text=True,
                   env={**os.environ, "ALPACA_API_KEY": "x", "ALPACA_SECRET_KEY": "y"})
check("B18 --ignore-earnings refused without --dry-run", r.returncode == 1 and "only runs with --dry-run" in r.stdout, r.stdout.strip())

with tempfile.TemporaryDirectory() as tmp:
    # the original rules are still one switch away
    saved = (bot.ENTRY_SIGNAL, bot.STRIKE_OFFSET, bot.DTE_MIN, bot.DTE_MAX, bot.MONTHLY_ONLY)
    bot.ENTRY_SIGNAL, bot.STRIKE_OFFSET, bot.DTE_MIN, bot.DTE_MAX, bot.MONTHLY_ONLY = "cross_below", 0.0, 45, 90, True
    f = signal_then_enter(tmp, crash="below")
    ok = bool(f.submitted)
    if ok:
        req = f.submitted[0]
        sk = int(req.legs[0].symbol[10:]) / 1000
        exp = datetime.strptime(req.legs[0].symbol[3:9], "%y%m%d").date()
        ok = exp == monthly(exp.year, exp.month) and 45 <= (exp - ENTRY_DAY).days <= 90 and sk == 150
    check("B19 original rules (cross below, no offset, 45-90 DTE monthlies) still work", ok,
          signals()[-1]["reason"] if signals() else "")
    bot.ENTRY_SIGNAL, bot.STRIKE_OFFSET, bot.DTE_MIN, bot.DTE_MAX, bot.MONTHLY_ONLY = saved

with tempfile.TemporaryDirectory() as tmp:
    TODAY = SIGNAL_DAY
    f = Fake(make_bars(SIGNAL_DAY, "below")); install(f, tmp)
    run("--signal")
    check("B20 variant A ignores a first close below the band (no signal recorded)", signals() == [])

print(f"\n{failures} failure(s)")
sys.exit(1 if failures else 0)
