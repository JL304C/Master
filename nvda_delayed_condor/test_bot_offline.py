"""
Offline test of nvda_condor_bot.py: replaces the Alpaca clients with fakes (real NVDA
weekly bars from nvda_weekly_adjusted.csv, a Black-Scholes option chain) and checks
each decision path, including that the multi-leg order requests pass alpaca-py's own
validation. No network, no keys needed:  python test_bot_offline.py
"""
import csv, math, os, sys, tempfile, types
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
sys.argv = [sys.argv[0]]
import nvda_condor_bot as bot
from alpaca.trading.enums import PositionIntent

HERE = Path(__file__).resolve().parent
TODAY = date(2026, 8, 28)          # a Friday on which the entry signal fired historically


class FakeDate(date):
    @classmethod
    def today(cls):
        return TODAY


bot.date = FakeDate


def weekly_until(d):
    rows = []
    with open(HERE / "nvda_weekly_adjusted.csv") as f:
        for r in csv.DictReader(f):
            k = float(r["adjusted close"]) / float(r["close"])
            rows.append(dict(d=date.fromisoformat(r["timestamp"]), o=float(r["open"]) * k,
                             h=float(r["high"]) * k, l=float(r["low"]) * k, c=float(r["adjusted close"])))
    rows.sort(key=lambda x: x["d"])
    return [r for r in rows if r["d"] <= d][-104:]


def bs(S, K, T, sig, call):
    d1 = (math.log(S / K) + 0.5 * sig * sig * T) / (sig * math.sqrt(T)); d2 = d1 - sig * math.sqrt(T)
    n = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))
    px = S * n(d1) - K * n(d2) if call else K * n(-d2) - S * n(-d1)
    return px, (n(d1) if call else n(d1) - 1)


class Fake:
    def __init__(self, bars, positions=(), open_orders=(), day=None):
        self.bars, self.positions, self.open_orders, self.day = bars, list(positions), list(open_orders), day
        self.submitted = []

    # --- stock data
    def get_stock_bars(self, req):
        if req.timeframe.value.endswith("Day") or str(req.timeframe) == "1Day":
            if not self.day:
                return {bot.SYMBOL: []}
            return {bot.SYMBOL: [types.SimpleNamespace(timestamp=datetime.combine(TODAY, datetime.min.time(), timezone.utc), **self.day)]}
        return {bot.SYMBOL: [types.SimpleNamespace(timestamp=datetime.combine(b["d"], datetime.min.time(), timezone.utc),
                                                   open=b["o"], high=b["h"], low=b["l"], close=b["c"]) for b in self.bars]}

    def get_stock_latest_trade(self, req):
        return {bot.SYMBOL: types.SimpleNamespace(price=self.bars[-1]["c"])}

    # --- trading
    def get_calendar(self, req):
        d = TODAY
        out = []
        while d <= req.end:
            if d.weekday() < 5:
                out.append(types.SimpleNamespace(date=d))
            d += timedelta(days=1)
        return out

    def get_option_contracts(self, req):
        lo = date.fromisoformat(req.expiration_date_gte); hi = date.fromisoformat(req.expiration_date_lte)
        exps, d = [], lo
        while d <= hi:
            if d.weekday() == 4:
                exps.append(types.SimpleNamespace(expiration_date=d))
            d += timedelta(days=1)
        return types.SimpleNamespace(option_contracts=exps, next_page_token=None)

    def get_all_positions(self):
        return [types.SimpleNamespace(symbol=s, qty=q) for s, q in self.positions]

    def get_orders(self, req):
        return self.open_orders

    def submit_order(self, req):
        self.submitted.append(req)
        return types.SimpleNamespace(id=f"order-{len(self.submitted)}")

    # --- option data
    def get_option_chain(self, req):
        S = self.bars[-1]["c"]; T = max((req.expiration_date - TODAY).days, 1) / 365
        call = req.type.value == "call"
        out = {}
        K = math.floor(req.strike_price_gte / 5) * 5
        while K <= req.strike_price_lte:
            sig = 0.45 * (1 - 0.8 * math.log(K / S)) if K < S else 0.45 * (1 + 0.2 * math.log(K / S))
            px, dl = bs(S, K, T, sig, call)
            sym = f"NVDA{req.expiration_date:%y%m%d}{'C' if call else 'P'}{int(K * 1000):08d}"
            out[sym] = types.SimpleNamespace(latest_quote=types.SimpleNamespace(bid_price=max(0.01, px - 0.03), ask_price=px + 0.03),
                                             greeks=types.SimpleNamespace(delta=dl))
            K += 5
        return out


def install(fake, tmp):
    bot.trade_client = bot.stock_client = bot.option_client = fake
    bot.LOG_JSONL = Path(tmp) / "log.jsonl"
    bot.LOG_CSV = Path(tmp) / "log.csv"
    bot.DRY_RUN = False


def last_log():
    import json
    return [json.loads(l) for l in bot.LOG_JSONL.read_text().splitlines()]


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        global failures
        failures += 1


failures = 0
bars = weekly_until(TODAY)
# tests 1-9 exercise the support-signal + delayed-call-side mode; 10-11 the default mode
DEFAULT_MODE = (bot.ENTRY_MODE, bot.ADD_CALL_SIDE)
bot.ENTRY_MODE, bot.ADD_CALL_SIDE = "support_signal", True

with tempfile.TemporaryDirectory() as tmp:
    # 1. flat, signal week -> opens a put spread
    f = Fake(bars); install(f, tmp); bot.run()
    ev = last_log()[-1]
    check("1 opens put spread on signal", ev["action"] == "open_put_spread", ev.get("reason"))
    if f.submitted:
        req = f.submitted[0]
        shortK = bot.parse_occ(req.legs[0].symbol)["strike"]; longK = bot.parse_occ(req.legs[1].symbol)["strike"]
        exp = bot.parse_occ(req.legs[0].symbol)["expiration"]
        check("1 order is mleg limit with negative (credit) price", req.order_class.value == "mleg" and req.limit_price < 0, str(req.limit_price))
        check("1 legs: STO short / BTO long, $20 wide", req.legs[0].position_intent == PositionIntent.SELL_TO_OPEN
              and req.legs[1].position_intent == PositionIntent.BUY_TO_OPEN and abs(shortK - longK - 20) < 1e-9, f"{shortK}/{longK}")
        check("1 short put <= support-2%", shortK <= ev["support"] * 0.98, f"{shortK} vs {ev['support']}")
        check("1 expiry before next earnings (2026-11-18) and >= 28 DTE",
              exp < date(2026, 11, 18) and (exp - TODAY).days >= 28, str(exp))
        check("1 qty == PUT_CONTRACTS", req.qty == bot.PUT_CONTRACTS)

with tempfile.TemporaryDirectory() as tmp:
    # 2. flat, not the last trading day of the week -> no entry check
    saved = TODAY; TODAY = date(2026, 8, 26)
    f = Fake(weekly_until(TODAY)); install(f, tmp); bot.run()
    check("2 mid-week: waits", last_log()[-1]["action"] == "wait" and not f.submitted)
    TODAY = saved

with tempfile.TemporaryDirectory() as tmp:
    # 3. pending order -> never stacks another
    f = Fake(bars, open_orders=[types.SimpleNamespace(symbol=None, legs=[types.SimpleNamespace(symbol="NVDA261009P00205000")])])
    install(f, tmp)
    bot.LOG_JSONL.write_text('{"action": "open_put_spread", "legs": "NVDA261009P00205000/NVDA261009P00185000", '
                             '"expiration": "2026-08-28", "underlying_price": 217.3}\n')
    bot.run()
    check("3 open order: waits, submits nothing", last_log()[-1]["action"] == "wait" and not f.submitted)

with tempfile.TemporaryDirectory() as tmp:
    # 4. earnings file stale -> refuses
    saved_files = bot.EARNINGS_FILES
    bot.EARNINGS_FILES = [HERE / "nvda_earnings_dates.txt"]
    f = Fake(bars); install(f, tmp); bot.run()
    check("4 no future earnings date: alert, no order", last_log()[-1]["action"] == "alert" and not f.submitted)
    bot.EARNINGS_FILES = saved_files

pos = [("NVDA261009P00205000", -2), ("NVDA261009P00185000", 2)]
OWN_PUTS = ('{"action": "open_put_spread", "legs": "NVDA261009P00205000/NVDA261009P00185000", '
            '"expiration": "2026-10-09", "underlying_price": 217.3}\n')
with tempfile.TemporaryDirectory() as tmp:
    # 5. put spread open, 20 DTE, near highs -> adds call spread on 50%
    saved = TODAY; TODAY = date(2026, 9, 18)
    f = Fake(weekly_until(TODAY), positions=pos, day=dict(high=223, low=215)); install(f, tmp)
    # the entry the bot logged on 2026-08-28 (NVDA 217.30); 222.27 now = risen > 2%
    bot.LOG_JSONL.write_text(OWN_PUTS)
    bot.run()
    ev = last_log()[-1]
    check("5 adds call spread", ev["action"] == "open_call_spread", ev.get("reason"))
    if f.submitted:
        req = f.submitted[0]
        check("5 call qty = 50% of puts (1)", req.qty == 1)
        check("5 short call delta < 0.20", ev["short_delta"] < 0.20, str(ev["short_delta"]))
        check("5 same expiry as puts", bot.parse_occ(req.legs[0].symbol)["expiration"] == date(2026, 10, 9))
    # 5b. rerun (position now includes call legs? not yet) -> must not add twice
    f2 = Fake(weekly_until(TODAY), positions=pos + [("NVDA261009C00250000", -1),
                                                    ("NVDA261009C00270000", 1)], day=dict(high=223, low=215)); install(f2, tmp)
    bot.run()
    check("5b never adds the call side twice", not f2.submitted, last_log()[-1]["action"])
    TODAY = saved

with tempfile.TemporaryDirectory() as tmp:
    # 6. price touches short put -> market close of the put spread
    saved = TODAY; TODAY = date(2026, 9, 18)
    f = Fake(weekly_until(TODAY), positions=pos, day=dict(high=215, low=204.5)); install(f, tmp)
    bot.LOG_JSONL.write_text(OWN_PUTS)
    bot.run()
    ev = last_log()[-1]
    check("6 touch closes put spread", ev["action"] == "close_spread" and f.submitted, ev.get("reason"))
    if f.submitted:
        req = f.submitted[0]
        check("6 close is mleg market BTC/STC", req.type.value == "market"
              and req.legs[0].position_intent == PositionIntent.BUY_TO_CLOSE
              and req.legs[1].position_intent == PositionIntent.SELL_TO_CLOSE and req.qty == 2)
    TODAY = saved

with tempfile.TemporaryDirectory() as tmp:
    # 7. dry run submits nothing
    f = Fake(bars); install(f, tmp); bot.DRY_RUN = True; bot.run()
    check("7 dry run: logs decision, submits nothing", last_log()[-1]["action"] == "open_put_spread" and not f.submitted)

with tempfile.TemporaryDirectory() as tmp:
    # 8. flat after an early close, original expiry still ahead -> no new entry yet
    f = Fake(bars); install(f, tmp)
    bot.LOG_JSONL.write_text('{"action": "open_put_spread", "expiration": "2026-09-04", "underlying_price": 200}\n')
    bot.run()
    check("8 waits out the prior cycle before re-entering", last_log()[-1]["action"] == "wait" and not f.submitted)

with tempfile.TemporaryDirectory() as tmp:
    # 9. the bull-call-spread bot's legs on the same account are ignored, never closed
    saved = TODAY; TODAY = date(2026, 9, 18)
    other = [("NVDA261023C00240000", 1), ("NVDA261023C00250000", -1)]
    f = Fake(weekly_until(TODAY), positions=other, day=dict(high=260, low=200)); install(f, tmp)
    bot.run()
    ev = [e for e in last_log() if e["action"] == "check"][-1]
    check("9 other bot's NVDA calls ignored (not closed, not treated as ours)",
          not f.submitted and "NVDA261023C00250000" in ev["reason"] and "legs={}" in ev["reason"], ev["reason"])
    f = Fake(weekly_until(TODAY), positions=pos + other, day=dict(high=260, low=215)); install(f, tmp)
    bot.LOG_JSONL.write_text(OWN_PUTS)
    bot.run()
    check("9b with both bots' legs present, only our own legs are managed",
          all("NVDA261023" not in l.symbol for r in f.submitted for l in r.legs), [l.symbol for r in f.submitted for l in r.legs])
    TODAY = saved

bot.ENTRY_MODE, bot.ADD_CALL_SIDE = DEFAULT_MODE
check("10 default settings: every_cycle, no call side", DEFAULT_MODE == ("every_cycle", False), str(DEFAULT_MODE))
with tempfile.TemporaryDirectory() as tmp:
    # 10b. every_cycle opens on a week-end with NO support signal (2026-09-11)
    saved = TODAY; TODAY = date(2026, 9, 11)
    wb = weekly_until(TODAY)
    check("10b (precondition) no support signal that week", bot.rules.entry_signal(wb, len(wb) - 1) is None)
    f = Fake(wb); install(f, tmp); bot.run()
    ev = last_log()[-1]
    check("10b every_cycle opens a put spread anyway", ev["action"] == "open_put_spread" and len(f.submitted) == 1, ev.get("reason"))
    if f.submitted:
        K = bot.parse_occ(f.submitted[0].legs[0].symbol)["strike"]
        check("10b short put ~7% OTM or lower", K <= wb[-1]["c"] * 0.95 * 0.98, f"{K} vs price {wb[-1]['c']:.2f}")
    TODAY = saved
with tempfile.TemporaryDirectory() as tmp:
    # 11. put spread open with 20 DTE and NVDA risen: default mode never adds calls
    saved = TODAY; TODAY = date(2026, 9, 18)
    f = Fake(weekly_until(TODAY), positions=pos, day=dict(high=223, low=215)); install(f, tmp)
    bot.LOG_JSONL.write_text(OWN_PUTS)
    bot.run()
    check("11 default mode: no call side added", not f.submitted and last_log()[-1]["action"] == "wait")
    TODAY = saved

print("\nALL PASS" if failures == 0 else f"\n{failures} FAILURE(S)")
sys.exit(1 if failures else 0)
