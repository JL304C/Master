"""
NVDA put credit spread / delayed iron condor -- scheduled paper-trading bot for Alpaca.

Trades the strategies backtested in nvda_daily_backtest.py / nvda_delayed_condor_backtest.py.
CURRENT SETTINGS (ENTRY_MODE="every_cycle", ADD_CALL_SIDE=False) = backtest variant D,
chosen for the most trades: a $20-wide put credit spread every cycle, no call side.

  1. On the last trading day of the week, with no spread open (and the previous
     position's cycle finished): sell a $20-wide put credit spread, ~45 DTE (shortened
     to the last weekly expiry before earnings, min 28 DTE), short put <= 0.30 delta and
       - every_cycle mode:    at or below (price x 0.95) x 0.98, i.e. ~7% out of the money
       - support_signal mode: only if NVDA is "declining but finding support"
                              (condor_rules.py), short put 2% below that support.
     Only if the credit is >= 10% of the width.
  2. Only if ADD_CALL_SIDE is True -- with 14-28 days left, if NVDA has risen / is near resistance / is overbought and
     no earnings fall before expiry: add a same-expiry $20-wide call credit spread,
     short call < 0.20 delta, on CALL_FRACTION of the put contracts (default 50%).
     Added at most once per position.
  3. If NVDA touches a short strike (today's low/high or the latest price), close that
     spread. On expiration day, close any spread whose short strike is within $1 of
     the price (avoids surprise assignment); otherwise let both expire.

State is derived from the Alpaca account each run (open option positions/orders), but
ONLY for contracts this bot opened, as recorded in its own log -- so it can share an
account with another NVDA options bot without touching that bot's positions. (Keep the
log file: it is how the bot recognises its own positions.) The log also supplies the
NVDA price at put entry (for the "has risen" test) and whether the call side was
already added.

Paper trading only: paper=True is hard-coded. Run once per trading day ~3:45 PM ET.
First run:  python nvda_condor_bot.py --dry-run   (decides and logs, submits nothing)

Requires: pip install alpaca-py
Credentials: ALPACA_API_KEY / ALPACA_SECRET_KEY env vars, or a .env file next to this script.
"""

from __future__ import annotations

import csv
import json
import os
import sys
from datetime import datetime, timedelta, date, timezone
from pathlib import Path

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    GetOptionContractsRequest,
    GetOrdersRequest,
    GetCalendarRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    OptionLegRequest,
)
from alpaca.trading.enums import (
    AssetStatus,
    ContractType,
    OrderClass,
    OrderSide,
    OrderType,
    PositionIntent,
    QueryOrderStatus,
    TimeInForce,
)
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionChainRequest, StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import Adjustment, DataFeed

import condor_rules as rules

# --------------------------------------------------------------------------- #
# Strategy parameters -- what was backtested. Change these and you are running
# a different, un-tested strategy.
# --------------------------------------------------------------------------- #
SYMBOL = "NVDA"
WIDTH = 20.0                  # spread width, both sides ($5 wide lost money after costs)
PUT_CONTRACTS = 2             # put spreads per position (backtest: <=5 for a $26k account)
ENTRY_MODE = "every_cycle"    # "every_cycle" (most trades, backtest D) or "support_signal"
EVERY_CYCLE_OTM = 0.05        # every_cycle: reference level = price x (1 - this)
ADD_CALL_SIDE = False         # True = delayed iron condor (add the call spread later)
CALL_FRACTION = 0.5           # call spreads = 50% of put contracts (if ADD_CALL_SIDE)
ACCOUNT_CAP = 26_000.0        # max loss of a new position must fit under this
TARGET_DTE = 45
MIN_DTE = 28                  # shortest expiry allowed when dodging earnings
SUPPORT_BUFFER = 0.98         # short put at or below support - 2%
MAX_PUT_DELTA = 0.30
MAX_CALL_DELTA = 0.20
MIN_PUT_CREDIT_PCT = 0.10     # skip if put credit < 10% of width
MIN_CALL_CREDIT = 0.05        # skip call add if credit is trivial
CALL_ADD_DTE = (14, 28)       # "2-4 weeks left"
EXPIRY_PIN_BUFFER = 1.00      # on expiry day, close a spread whose short strike is this close
STOCK_FEED = DataFeed.IEX     # free Alpaca data plan; use DataFeed.SIP if you pay for it

HERE = Path(__file__).resolve().parent
LOG_JSONL = HERE / "nvda_condor_log.jsonl"
LOG_CSV = HERE / "nvda_condor_trades.csv"
EARNINGS_FILES = [HERE / "nvda_earnings_dates.txt", HERE / "nvda_upcoming_earnings.txt"]

DRY_RUN = "--dry-run" in sys.argv


def load_env_file(path: Path) -> None:
    """Minimal .env loader so this script has no extra dependency for it."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


load_env_file(HERE / ".env")

API_KEY = os.environ.get("ALPACA_API_KEY")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
if not API_KEY or not SECRET_KEY:
    print("Missing ALPACA_API_KEY / ALPACA_SECRET_KEY (env var or .env file next to this script).")
    sys.exit(1)

# paper=True is hard-coded here on purpose -- this script is not the place a
# live decision gets made.
trade_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
stock_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)
option_client = OptionHistoricalDataClient(API_KEY, SECRET_KEY)


# --------------------------------------------------------------------------- #
# logging
# --------------------------------------------------------------------------- #
def log(event: dict) -> None:
    event = {"ts": datetime.now(timezone.utc).isoformat(), "dry_run": DRY_RUN, **event}
    with LOG_JSONL.open("a") as fh:
        fh.write(json.dumps(event, default=str) + "\n")
    is_new = not LOG_CSV.exists()
    with LOG_CSV.open("a", newline="") as fh:
        w = csv.writer(fh)
        if is_new:
            w.writerow(["timestamp", "dry_run", "action", "legs", "qty", "reason"])
        w.writerow([event["ts"], DRY_RUN, event.get("action", ""), event.get("legs", ""),
                    event.get("qty", ""), event.get("reason", "")])
    print(json.dumps(event, indent=2, default=str))


def past_events(action: str) -> list[dict]:
    if not LOG_JSONL.exists():
        return []
    out = []
    for line in LOG_JSONL.read_text().splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("action") == action and not e.get("dry_run"):
            out.append(e)
    return out


# --------------------------------------------------------------------------- #
# market data
# --------------------------------------------------------------------------- #
def as_date(x) -> date:
    return x if isinstance(x, date) and not isinstance(x, datetime) else (
        x.date() if isinstance(x, datetime) else datetime.strptime(str(x), "%Y-%m-%d").date())


def weekly_bars() -> list[dict]:
    """~2 years of split-adjusted weekly bars, oldest first. The last bar is the
    current (possibly partial) week."""
    req = StockBarsRequest(symbol_or_symbols=SYMBOL, timeframe=TimeFrame.Week,
                           start=datetime.now(timezone.utc) - timedelta(days=730),
                           adjustment=Adjustment.ALL, feed=STOCK_FEED)
    bars = stock_client.get_stock_bars(req)[SYMBOL]
    return [dict(d=b.timestamp.date(), o=float(b.open), h=float(b.high),
                 l=float(b.low), c=float(b.close)) for b in bars]


def today_bar() -> dict | None:
    req = StockBarsRequest(symbol_or_symbols=SYMBOL, timeframe=TimeFrame.Day,
                           start=datetime.now(timezone.utc) - timedelta(days=5),
                           adjustment=Adjustment.ALL, feed=STOCK_FEED)
    bars = stock_client.get_stock_bars(req)[SYMBOL]
    if not bars:
        return None
    b = bars[-1]
    if b.timestamp.date() != date.today():
        return None
    return dict(h=float(b.high), l=float(b.low))


def last_price() -> float:
    t = stock_client.get_stock_latest_trade(
        StockLatestTradeRequest(symbol_or_symbols=SYMBOL, feed=STOCK_FEED))[SYMBOL]
    return float(t.price)


def is_last_trading_day_of_week(today: date) -> bool:
    cal = trade_client.get_calendar(GetCalendarRequest(start=today, end=today + timedelta(days=7)))
    days = sorted(as_date(c.date) for c in cal)
    if not days or days[0] != today:
        return False                      # market closed today
    nxt = days[1] if len(days) > 1 else None
    return nxt is None or nxt.isocalendar()[1] != today.isocalendar()[1]


def option_expirations(dte_min: int, dte_max: int) -> list[date]:
    today = date.today()
    req = GetOptionContractsRequest(
        underlying_symbols=[SYMBOL], status=AssetStatus.ACTIVE, type=ContractType.PUT,
        expiration_date_gte=(today + timedelta(days=dte_min)).isoformat(),
        expiration_date_lte=(today + timedelta(days=dte_max)).isoformat(),
        limit=10000,
    )
    exps = set()
    while True:
        resp = trade_client.get_option_contracts(req)
        for c in resp.option_contracts or []:
            exps.add(as_date(c.expiration_date))
        if not resp.next_page_token:
            break
        req.page_token = resp.next_page_token
    return sorted(exps)


def chain(exp: date, right: ContractType, k_lo: float, k_hi: float) -> list[dict]:
    """Snapshot chain for one expiry: [{symbol, strike, bid, ask, mid, delta}], by strike."""
    snaps = option_client.get_option_chain(OptionChainRequest(
        underlying_symbol=SYMBOL, type=right, expiration_date=exp,
        strike_price_gte=k_lo, strike_price_lte=k_hi))
    rows = []
    for sym, s in snaps.items():
        q = s.latest_quote
        if q is None or not q.bid_price or not q.ask_price:
            continue
        delta = s.greeks.delta if s.greeks is not None else None
        rows.append(dict(symbol=sym, strike=parse_occ(sym)["strike"], bid=float(q.bid_price),
                         ask=float(q.ask_price), mid=(q.bid_price + q.ask_price) / 2,
                         delta=None if delta is None else float(delta)))
    return sorted(rows, key=lambda r: r["strike"])


def parse_occ(occ: str) -> dict:
    """NVDA261106P00200000 -> underlying/expiration/right/strike."""
    i = 0
    while i < len(occ) and not occ[i].isdigit():
        i += 1
    rest = occ[i:]
    return {"underlying": occ[:i], "expiration": datetime.strptime(rest[:6], "%y%m%d").date(),
            "right": rest[6], "strike": int(rest[7:]) / 1000.0}


# --------------------------------------------------------------------------- #
# account state
# --------------------------------------------------------------------------- #
def own_symbols() -> set[str]:
    """Option symbols THIS bot opened (from its own log). Anything else on the account --
    e.g. the NVDA bull call spread bot's legs -- is never read or touched by this bot."""
    out = set()
    for action in ("open_put_spread", "open_call_spread"):
        for e in past_events(action):
            out.update(e.get("legs", "").split("/"))
    out.discard("")
    return out


def is_nvda_option(sym: str) -> bool:
    return sym.startswith(SYMBOL) and len(sym) > len(SYMBOL) + 6


def option_positions(mine: set[str]) -> tuple[dict, list[str]]:
    """Groups this bot's NVDA option legs: {'short_put','long_put','short_call','long_call'}
    -> dict(symbol, strike, expiration, qty) (qty positive). Also returns the NVDA option
    symbols held that this bot did NOT open (left alone)."""
    legs, foreign = {}, []
    for p in trade_client.get_all_positions():
        if not is_nvda_option(p.symbol):
            continue
        if p.symbol not in mine:
            foreign.append(p.symbol)
            continue
        o = parse_occ(p.symbol)
        if o["underlying"] != SYMBOL:
            continue
        qty = int(float(p.qty))
        key = ("short_" if qty < 0 else "long_") + ("put" if o["right"] == "P" else "call")
        if key in legs:
            legs.setdefault("extra", []).append(p.symbol)
            continue
        legs[key] = dict(symbol=p.symbol, strike=o["strike"], expiration=o["expiration"], qty=abs(qty))
    return legs, foreign


def open_option_orders(mine: set[str]) -> list:
    """Still-open orders on this bot's own contracts (another bot's orders are ignored)."""
    orders = trade_client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=True))
    out = []
    for o in orders:
        syms = [o.symbol or ""] + [l.symbol for l in (o.legs or [])]
        if any(sym in mine for sym in syms):
            out.append(o)
    return out


# --------------------------------------------------------------------------- #
# orders
# --------------------------------------------------------------------------- #
def submit_spread(legs: list[tuple[str, PositionIntent]], qty: int,
                  net_credit: float | None):
    """Two-leg order. net_credit given -> DAY limit at that credit; None -> market (used
    for defensive closes, where getting out matters more than the price).
    Alpaca multi-leg convention: limit_price NEGATIVE = net credit, positive = net debit.
    (If that convention were ever reversed, a negative price is rejected rather than
    filled at a bad price -- the safe failure mode. Verify on the first paper fill.)"""
    leg_reqs = [OptionLegRequest(symbol=s, ratio_qty=1, position_intent=pi,
                                 side=OrderSide.SELL if pi in (PositionIntent.SELL_TO_OPEN, PositionIntent.SELL_TO_CLOSE) else OrderSide.BUY)
                for s, pi in legs]
    if net_credit is None:
        req = MarketOrderRequest(qty=qty, order_class=OrderClass.MLEG, legs=leg_reqs,
                                 time_in_force=TimeInForce.DAY)
    else:
        req = LimitOrderRequest(qty=qty, order_class=OrderClass.MLEG, legs=leg_reqs,
                                time_in_force=TimeInForce.DAY,
                                limit_price=round(-abs(net_credit), 2))
    order_id = None
    if not DRY_RUN:
        order_id = trade_client.submit_order(req).id
    return order_id


# --------------------------------------------------------------------------- #
# strategy steps
# --------------------------------------------------------------------------- #
def pick_expiry(earnings: list[date]) -> date | None:
    """Expiry closest to 45 DTE with no earnings on/before it; shortens toward 28 DTE
    to dodge a report. None if impossible or if the earnings file is not current."""
    today = date.today()
    for exp in sorted(option_expirations(MIN_DTE, TARGET_DTE + 7),
                      key=lambda e: abs((e - today).days - TARGET_DTE)):
        if not any(e > exp for e in earnings):
            continue                         # can't prove earnings-free: file not current
        if rules.earnings_between(earnings, today, exp):
            continue
        return exp
    return None


def try_open_put_spread(bars, price, earnings):
    i = len(bars) - 1
    if ENTRY_MODE == "every_cycle":
        sup = price * (1 - EVERY_CYCLE_OTM)
    else:
        sup = rules.entry_signal(bars, i)
        if sup is None:
            log({"action": "no_signal", "reason": f"price={price:.2f}: not declining-into-support this week"})
            return
    if not any(e > date.today() + timedelta(days=TARGET_DTE) for e in earnings):
        log({"action": "alert", "reason": "ready to open but nvda_upcoming_earnings.txt has no date "
             "beyond the trade window -- update it, then rerun"})
        return
    exp = pick_expiry(earnings)
    if exp is None:
        log({"action": "skip_put", "reason": f"signal (support {sup:.2f}) but no expiry in "
             f"{MIN_DTE}-{TARGET_DTE + 7} DTE avoids earnings"})
        return
    puts = chain(exp, ContractType.PUT, sup * 0.60, sup)
    shorts = [r for r in puts if r["strike"] <= sup * SUPPORT_BUFFER
              and r["delta"] is not None and -r["delta"] <= MAX_PUT_DELTA]
    if not shorts:
        log({"action": "skip_put", "reason": f"no put <= {sup * SUPPORT_BUFFER:.2f} with delta <= {MAX_PUT_DELTA} (or no greeks)"})
        return
    short = shorts[-1]                                   # highest qualifying strike
    long = next((r for r in puts if abs(r["strike"] - (short["strike"] - WIDTH)) < 0.01), None)
    if long is None:
        log({"action": "skip_put", "reason": f"no {short['strike'] - WIDTH} put to buy for a ${WIDTH:.0f} spread"})
        return
    credit = round(short["mid"] - long["mid"], 2)
    max_loss = (WIDTH - credit) * 100 * PUT_CONTRACTS
    if credit < MIN_PUT_CREDIT_PCT * WIDTH:
        log({"action": "skip_put", "reason": f"credit {credit:.2f} < {MIN_PUT_CREDIT_PCT:.0%} of width"})
        return
    if max_loss > ACCOUNT_CAP:
        log({"action": "skip_put", "reason": f"max loss ${max_loss:,.0f} over ${ACCOUNT_CAP:,.0f} cap"})
        return
    oid = submit_spread(
                        [(short["symbol"], PositionIntent.SELL_TO_OPEN),
                         (long["symbol"], PositionIntent.BUY_TO_OPEN)],
                        PUT_CONTRACTS, credit)
    log({"action": "open_put_spread", "legs": f"{short['symbol']}/{long['symbol']}", "qty": PUT_CONTRACTS,
         "expiration": exp.isoformat(), "underlying_price": price, "support": round(sup, 2), "mode": ENTRY_MODE,
         "limit_credit": credit, "short_delta": short["delta"], "buying_power": max_loss,
         "order_id": order_id_str(oid),
         "reason": f"{ENTRY_MODE} ref {sup:.2f}; short {short['strike']} (delta {short['delta']:.2f}); "
                   f"credit {credit:.2f} x{PUT_CONTRACTS}; BP ${max_loss:,.0f}"})


def try_add_call_spread(bars, price, legs, earnings):
    sp = legs["short_put"]
    exp = sp["expiration"]
    dte = (exp - date.today()).days
    if not (CALL_ADD_DTE[0] <= dte <= CALL_ADD_DTE[1]):
        return
    if any(e.get("expiration") == exp.isoformat() for e in past_events("open_call_spread")):
        log({"action": "wait", "reason": f"call side already added once for {exp}; not re-adding"})
        return
    if rules.earnings_between(earnings, date.today(), exp):
        log({"action": "skip_call", "reason": f"earnings before {exp} -- no call side"})
        return
    entry = [e for e in past_events("open_put_spread") if e.get("expiration") == exp.isoformat()]
    entry_price = entry[-1]["underlying_price"] if entry else None
    if not rules.call_add_signal(bars, len(bars) - 1, entry_price):
        log({"action": "wait", "reason": f"{dte} DTE: not risen/near resistance/overbought yet (entry {entry_price})"})
        return
    calls = chain(exp, ContractType.CALL, price, price * 1.6)
    shorts = [r for r in calls if r["strike"] > price and r["delta"] is not None and r["delta"] < MAX_CALL_DELTA]
    if not shorts:
        log({"action": "skip_call", "reason": f"no call with delta < {MAX_CALL_DELTA} (or no greeks)"})
        return
    short = shorts[0]                                    # lowest strike under the delta cap
    long = next((r for r in calls if abs(r["strike"] - (short["strike"] + WIDTH)) < 0.01), None)
    if long is None:
        log({"action": "skip_call", "reason": f"no {short['strike'] + WIDTH} call for a ${WIDTH:.0f} spread"})
        return
    credit = round(short["mid"] - long["mid"], 2)
    if credit < MIN_CALL_CREDIT:
        log({"action": "skip_call", "reason": f"call credit {credit:.2f} too small"})
        return
    qty = max(1, round(sp["qty"] * CALL_FRACTION))
    oid = submit_spread(
                        [(short["symbol"], PositionIntent.SELL_TO_OPEN),
                         (long["symbol"], PositionIntent.BUY_TO_OPEN)],
                        qty, credit)
    log({"action": "open_call_spread", "legs": f"{short['symbol']}/{long['symbol']}", "qty": qty,
         "expiration": exp.isoformat(), "underlying_price": price, "limit_credit": credit,
         "short_delta": short["delta"], "order_id": order_id_str(oid),
         "reason": f"{dte} DTE; short call {short['strike']} (delta {short['delta']:.2f}); credit {credit:.2f} x{qty}"})


def close_spread(short: dict, long: dict | None, why: str):
    legs = [(short["symbol"], PositionIntent.BUY_TO_CLOSE)]
    if long:
        legs.append((long["symbol"], PositionIntent.SELL_TO_CLOSE))
    if len(legs) == 1:
        log({"action": "alert", "legs": short["symbol"], "reason": f"{why} -- short leg has no matching long leg; close it manually"})
        return
    oid = submit_spread(legs, short["qty"], None)
    log({"action": "close_spread", "legs": f"{short['symbol']}/{long['symbol']}", "qty": short["qty"],
         "order_id": order_id_str(oid), "reason": why})


def order_id_str(oid):
    return str(oid) if oid else ("DRY-RUN" if DRY_RUN else None)


def manage(legs, price, day):
    """Close a threatened side; returns True if it submitted anything."""
    lo = min(price, day["l"]) if day else price
    hi = max(price, day["h"]) if day else price
    acted = False
    sp, lp, sc, lc = (legs.get(k) for k in ("short_put", "long_put", "short_call", "long_call"))
    exp = (sp or sc)["expiration"]
    on_expiry_day = exp == date.today()
    if sp and (lo <= sp["strike"] or (on_expiry_day and price <= sp["strike"] + EXPIRY_PIN_BUFFER)):
        close_spread(sp, lp, f"put side threatened: low {lo:.2f} vs short put {sp['strike']}"
                     + (" (expiry day)" if on_expiry_day else ""))
        acted = True
    if sc and (hi >= sc["strike"] or (on_expiry_day and price >= sc["strike"] - EXPIRY_PIN_BUFFER)):
        close_spread(sc, lc, f"call side threatened: high {hi:.2f} vs short call {sc['strike']}"
                     + (" (expiry day)" if on_expiry_day else ""))
        acted = True
    return acted


def run():
    today = date.today()
    earnings = sorted(set(sum((rules.load_earnings(p) for p in EARNINGS_FILES if p.exists()), [])))
    price = last_price()
    mine = own_symbols()
    legs, foreign = option_positions(mine)
    pending = open_option_orders(mine)
    log({"action": "check", "reason": f"price={price:.2f} legs={ {k: v['symbol'] for k, v in legs.items() if k != 'extra'} } "
                                      f"open_orders={len(pending)} ignored_other_nvda_options={foreign}"})

    if pending:
        log({"action": "wait", "reason": f"{len(pending)} NVDA option order(s) still open -- not stacking another"})
        return
    if "extra" in legs:
        log({"action": "alert", "reason": f"more than one position per leg type ({legs['extra']}) -- "
                                          "the bot manages one condor at a time; resolve manually"})
        return

    if legs:
        exps = {v["expiration"] for k, v in legs.items()}
        if len(exps) > 1:
            log({"action": "alert", "reason": f"legs on different expirations {exps} -- resolve manually"})
            return
        if manage(legs, price, today_bar()):
            return
        if ADD_CALL_SIDE and "short_put" in legs and "short_call" not in legs:
            try_add_call_spread(weekly_bars(), price, legs, earnings)
        else:
            log({"action": "wait", "reason": "position open, nothing to do"})
        return

    # flat. Like the backtest, one position per cycle: after a spread is closed early,
    # wait for its original expiration before looking for a new entry.
    live = [e["expiration"] for e in past_events("open_put_spread")
            if date.fromisoformat(e["expiration"]) >= today]
    if live:
        log({"action": "wait", "reason": f"flat, but the last position's cycle runs to {max(live)}"})
        return
    # new entries only on the week's last trading day (the backtest used weekly closes)
    if not is_last_trading_day_of_week(today) and "--force-signal-check" not in sys.argv:
        log({"action": "wait", "reason": "flat; entry signal is only checked on the last trading day of the week"})
        return
    try_open_put_spread(weekly_bars(), price, earnings)


if __name__ == "__main__":
    run()
