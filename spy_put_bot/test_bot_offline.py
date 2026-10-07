"""Offline checks for spy_put_bot.py -- no keys, no network. Run: python -m pytest -q test_bot_offline.py"""
import json
from datetime import date, timedelta

import pytest

import spy_put_bot as bot


class FakeBroker:
    def __init__(self, today, price=650.0):
        self.today, self.price = today, price
        self.holidays = set()
        self.pos, self.orders, self.order_status, self.quote = [], [], {}, {}
        self.submitted = []
        self.exps = [today + timedelta(days=d) for d in (63, 84, 91, 112)]

    def trading_days(self, start, end):
        return [start + timedelta(days=i) for i in range((end - start).days + 1)
                if (start + timedelta(days=i)).weekday() < 5 and start + timedelta(days=i) not in self.holidays]

    def last_price(self):
        return self.price

    def positions(self):
        return list(self.pos)

    def open_orders(self):
        return list(self.orders)

    def order(self, oid):
        return self.order_status.get(oid)

    def put_expirations(self, first, last):
        return [e for e in self.exps if first <= e <= last]

    def put_chain(self, exp, k_lo, k_hi):
        rows = []
        for k in range(int(k_lo), int(k_hi) + 1, 5):
            d = -0.5 * (k / self.price) ** 18           # ~-0.10 near 92% of spot
            px = round(80 * -d, 2)
            rows.append(dict(symbol=f"SPY{exp:%y%m%d}P{k * 1000:08d}", strike=float(k), bid=px - 0.02,
                             ask=px + 0.02, mid=px, delta=d))
        return rows

    def quotes(self, symbols):
        return {s: self.quote[s] for s in symbols if s in self.quote}

    def submit_limit(self, symbol, side, qty, limit, opening):
        oid = f"oid{len(self.submitted) + 1}"
        self.submitted.append(dict(symbol=symbol, side=side, qty=qty, limit=round(limit, 2), opening=opening, id=oid))
        return oid


@pytest.fixture
def log(tmp_path):
    return bot.Log(tmp_path / "log.jsonl", tmp_path / "log.csv", echo=False)


def actions(log):
    return [json.loads(x)["action"] for x in log.jsonl.read_text().splitlines()]


MONDAY = date(2026, 10, 12)


def test_monday_entry_picks_90dte_10delta(log):
    b = FakeBroker(MONDAY)
    bot.run(b, log, MONDAY)
    assert len(b.submitted) == 1
    o = b.submitted[0]
    assert o["side"] == "sell" and o["opening"] and o["qty"] == 1
    e = log.events("open_put")[-1]
    assert e["dte"] == 91                                  # closest to 90
    assert abs(e["delta"] - (-0.10)) < 0.02
    assert o["limit"] >= e["bid"]                          # never below the bid


def test_one_entry_per_week_and_retry_rules(log):
    b = FakeBroker(MONDAY)
    bot.run(b, log, MONDAY)
    b.order_status["oid1"] = dict(status="filled", filled_qty=1, filled_avg_price=4.0)
    bot.run(b, log, MONDAY + timedelta(days=1))           # Tuesday: already done this week
    assert len(b.submitted) == 1
    # a new week: Monday order expires unfilled -> Tuesday retries, Thursday doesn't
    b2, nxt = FakeBroker(MONDAY), MONDAY + timedelta(days=7)
    b2.order_status = {"oid1": dict(status="filled", filled_qty=1, filled_avg_price=4.0)}
    bot.run(b2, log, nxt)
    b2.order_status[b2.submitted[-1]["id"]] = dict(status="expired", filled_qty=0, filled_avg_price=None)
    bot.run(b2, log, nxt + timedelta(days=1))
    assert len(b2.submitted) == 2
    b2.order_status[b2.submitted[-1]["id"]] = dict(status="expired", filled_qty=0, filled_avg_price=None)
    bot.run(b2, log, nxt + timedelta(days=3))              # Thursday: too late
    assert len(b2.submitted) == 2


def test_missed_monday_holiday_enters_tuesday(log):
    b = FakeBroker(MONDAY)
    b.holidays.add(MONDAY)
    bot.run(b, log, MONDAY + timedelta(days=1))
    assert len(b.submitted) == 1
    assert "retry" not in log.events("open_put")[-1]["reason"]   # Tuesday is the week's first day


def _held(b, log, sym, credit, qty=-1):
    log({"action": "open_put", "trade_date": "2026-09-01", "symbol": sym, "order_id": "old"})
    b.order_status["old"] = dict(status="filled", filled_qty=1, filled_avg_price=credit)
    b.pos.append(dict(symbol=sym, qty=qty, avg_entry_price=credit))


@pytest.mark.parametrize("bid,ask,dte,reason,limit", [
    (1.90, 1.96, 60, "profit_target", 1.96),     # mid 1.93 <= 50% of 4.00
    (12.00, 12.10, 60, "stop_loss", 12.20),      # mid >= 3x 4.00 -> ask + 0.10
    (3.00, 3.10, 21, "time_21dte", 3.10),
    (3.00, 3.10, 22, None, None),
])
def test_exits(log, bid, ask, dte, reason, limit):
    b = FakeBroker(MONDAY + timedelta(days=2))            # Wednesday
    exp = b.today + timedelta(days=dte)
    sym = f"SPY{exp:%y%m%d}P00585000"
    _held(b, log, sym, 4.00)
    log({"action": "open_put", "trade_date": MONDAY.isoformat(), "symbol": "SPY-this-week", "order_id": "wk"})
    b.order_status["wk"] = dict(status="filled", filled_qty=1, filled_avg_price=4.0)
    b.quote[sym] = (bid, ask)
    bot.run(b, log, b.today)
    closes = [o for o in b.submitted if not o["opening"]]
    if reason is None:
        assert closes == []
    else:
        assert closes == [dict(symbol=sym, side="buy", qty=1, limit=limit, opening=False, id="oid1")]
        assert log.events("close_put")[-1]["exit_reason"] == reason


def test_leaves_other_bots_alone(log):
    b = FakeBroker(MONDAY + timedelta(days=2))
    nvda = "NVDA261120P00160000"
    other_spy = f"SPY{b.today + timedelta(days=30):%y%m%d}P00500000"
    b.pos += [dict(symbol=nvda, qty=-2, avg_entry_price=1.0), dict(symbol=other_spy, qty=-1, avg_entry_price=9.0)]
    b.orders.append(dict(id="x", symbol=nvda, side="buy"))
    b.quote[nvda] = (0.01, 0.02)                           # would hit "profit target" if it were ours
    b.quote[other_spy] = (0.01, 0.02)
    bot.run(b, log, b.today)
    assert all(o["opening"] for o in b.submitted)          # only this week's own entry, no closes


def test_working_order_blocks_duplicate_close(log):
    b = FakeBroker(MONDAY + timedelta(days=2))
    sym = f"SPY{b.today + timedelta(days=60):%y%m%d}P00585000"
    _held(b, log, sym, 4.00)
    b.orders.append(dict(id="c1", symbol=sym, side="buy"))
    b.quote[sym] = (1.0, 1.02)
    bot.run(b, log, b.today)
    assert not [o for o in b.submitted if not o["opening"]]


def test_caps_and_dry_run(tmp_path):
    b = FakeBroker(MONDAY)
    log = bot.Log(tmp_path / "l.jsonl", tmp_path / "l.csv", echo=False)
    for i in range(13):                                    # 13 puts open -> position cap
        _held(b, log, f"SPY{MONDAY + timedelta(days=40 + i):%y%m%d}P00590000", 4.0)
        b.quote[b.pos[-1]["symbol"]] = (3.0, 3.1)
    bot.run(b, log, MONDAY)
    assert not [o for o in b.submitted if o["opening"]]
    assert "cap is 13" in log.events("skip_entry")[-1]["reason"]

    # 12 puts at a $700 strike = $840k secured -> one more would pass the $850k cash cap
    b3 = FakeBroker(MONDAY, price=780.0)
    log3 = bot.Log(tmp_path / "c.jsonl", tmp_path / "c.csv", echo=False)
    for i in range(12):
        _held(b3, log3, f"SPY{MONDAY + timedelta(days=40 + i):%y%m%d}P00700000", 4.0)
        b3.quote[b3.pos[-1]["symbol"]] = (3.0, 3.1)
    bot.run(b3, log3, MONDAY)
    assert not [o for o in b3.submitted if o["opening"]]
    assert "cash secured" in log3.events("skip_entry")[-1]["reason"]

    dry = bot.Log(tmp_path / "d.jsonl", tmp_path / "d.csv", dry_run=True, echo=False)
    b2 = FakeBroker(MONDAY)
    bot.run(b2, dry, MONDAY, dry_run=True)
    assert b2.submitted == [] and "open_put" in actions(dry)
    assert dry.events("open_put") == []                    # dry-run entries never count as real


def test_not_a_trading_day(log):
    b = FakeBroker(MONDAY)
    bot.run(b, log, MONDAY + timedelta(days=5))            # Saturday
    assert b.submitted == [] and actions(log) == ["wait"]


def test_parse_occ_and_filter():
    o = bot.parse_occ("SPY261218P00585000")
    assert o == {"underlying": "SPY", "expiration": date(2026, 12, 18), "right": "P", "strike": 585.0}
    assert bot.is_spy_option("SPY261218P00585000") and not bot.is_spy_option("SPYG261218P00085000")


@pytest.mark.parametrize("bid,ask", [(0.0, 30.0), (5.0, 30.0)])
def test_bad_quote_never_triggers_an_exit(log, bid, ask):
    b = FakeBroker(MONDAY + timedelta(days=2))
    sym = f"SPY{b.today + timedelta(days=60):%y%m%d}P00585000"
    _held(b, log, sym, 4.00)
    b.quote[sym] = (bid, ask)                             # mid would look like a 3x stop
    bot.run(b, log, b.today)
    assert not [o for o in b.submitted if not o["opening"]]
    assert "looks bad" in [json.loads(x) for x in log.jsonl.read_text().splitlines()
                           if json.loads(x)["action"] == "alert"][-1]["reason"]
