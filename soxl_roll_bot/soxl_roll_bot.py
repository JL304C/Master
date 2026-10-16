"""
SOXL weekly put seller with same-strike rolling -- paper-trading bot for Alpaca.

Implements "Option 6" from soxl_put_backtest/variants.py:
  - Every week, on the last trading day of the week (normally Friday) near the
    close, sell cash-secured puts on SOXL expiring next week (~7 DTE).
  - Strike = SOXL price - expected move of the expiration ~2x further out
    (~14 DTE), rounded DOWN to the nearest listed strike. Take whatever
    premium that strike pays (premium rule A).
    Expected move = price * IV * sqrt(DTE/365) * 0.798 (thinkorswim's figure),
    IV = at-the-money implied volatility of that ~14 DTE expiration.
  - On expiration day, the expiring put is always bought back before the close
    (pennies when out of the money; this also removes after-hours pin risk).
      * If it finished out of the money: sell a new put at the rule strike.
      * If SOXL is BELOW the strike (in the money): ROLL -- buy it back and
        sell next week's put at the SAME strike, same number of contracts.
        Shares are never taken on purpose.
  - If shares show up anyway (early assignment), sell them and roll the put
    at the assignment price (the shares' average entry price).

Derives state from Alpaca every run (positions, open orders). The only local
state is pending_roll.json, written between buying back an in-the-money put
and selling its replacement, so a failure in between still rolls at the same
strike on the next run.

Schedule it every weekday at ~3:40 PM US Eastern. On any other day than the
last trading day of the week it only logs status. Outside 3:30-3:58 PM ET it
refuses to trade unless run with --force.

Paper trading only: paper=True is hard-coded.

Usage:
  python soxl_roll_bot.py            # normal scheduled run
  python soxl_roll_bot.py --dry-run  # show what it would do, place no orders
  python soxl_roll_bot.py --force    # ignore the day/time checks (testing)

Requires: pip install alpaca-py
Credentials: ALPACA_API_KEY / ALPACA_SECRET_KEY as env vars or in a .env file
next to this script.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# --------------------------------------------------------------------------- #
# Strategy parameters -- what was backtested. Change these and you are running
# a different, untested strategy.
# --------------------------------------------------------------------------- #
SYMBOL = "SOXL"
ACCOUNT_CAP = 26_000.0      # max cash reserved for puts (strike * 100 * contracts)
MAX_CONTRACTS = 10          # hard ceiling regardless of cap
TRADE_DTE = (4, 10, 7)      # trade expiration: DTE window and target
REF_DTE = (10, 18)          # reference expiration window; target = 2x trade DTE
EM_FACTOR = 0.798           # sqrt(2/pi): makes EM ~= ATM straddle price
PREMIUM_TARGET = 83.0       # only logged; rule A takes whatever is offered
TRADE_WINDOW_ET = ((15, 30), (15, 58))

# Order working: start at mid, step toward the far side, give up after the last.
FILL_STEPS = (0.0, 0.5, 1.0)   # fraction of the way from mid to bid (sell) / ask (buy)
FILL_WAIT_SEC = 20

HERE = Path(__file__).resolve().parent
LOG_JSONL = HERE / "soxl_roll_log.jsonl"
LOG_CSV = HERE / "soxl_roll_trades.csv"
PENDING_ROLL = HERE / "pending_roll.json"
ET = ZoneInfo("America/New_York")


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested in test_offline.py, no network)
# --------------------------------------------------------------------------- #
def parse_occ_symbol(occ: str) -> dict:
    """SOXL261023P00120000 -> underlying/expiration/right/strike."""
    i = len(occ) - 15
    return {
        "underlying": occ[:i],
        "expiration": datetime.strptime(occ[i:i + 6], "%y%m%d").date(),
        "right": occ[i + 6],
        "strike": int(occ[i + 7:]) / 1000.0,
    }


def pick_expirations(expirations, today: date):
    """Trade expiration closest to 7 DTE (4..10), reference expiration closest
    to 2x its DTE (10..18). Returns (trade, ref); either may be None."""
    lo, hi, tgt = TRADE_DTE
    future = sorted(e for e in set(expirations) if e > today)
    trade = [e for e in future if lo <= (e - today).days <= hi]
    if not trade:
        return None, None
    t = min(trade, key=lambda e: abs((e - today).days - tgt))
    ref = [e for e in future if REF_DTE[0] <= (e - today).days <= REF_DTE[1] and e > t]
    if not ref:
        return t, None
    t_dte = (t - today).days
    return t, min(ref, key=lambda e: abs((e - today).days - 2 * t_dte))


def _ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_price(S: float, K: float, T: float, vol: float, right: str) -> float:
    if T <= 0 or vol <= 0:
        return max(0.0, S - K) if right == "C" else max(0.0, K - S)
    d1 = (math.log(S / K) + 0.5 * vol * vol * T) / (vol * math.sqrt(T))
    d2 = d1 - vol * math.sqrt(T)
    if right == "C":
        return S * _ncdf(d1) - K * _ncdf(d2)
    return K * _ncdf(-d2) - S * _ncdf(-d1)


def implied_vol(px: float, S: float, K: float, T: float, right: str):
    lo, hi = 1e-3, 10.0
    if not (bs_price(S, K, T, lo, right) < px < bs_price(S, K, T, hi, right)):
        return None
    for _ in range(100):
        mid = (lo + hi) / 2
        if bs_price(S, K, T, mid, right) < px:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def expected_move(price: float, quotes: dict, ref_exp: date, today: date):
    """quotes: {(right, strike): (bid, ask)} for the reference expiration.
    Returns (em, iv) from the at-the-money call/put IV, or (None, None)."""
    strikes = {k for (r, k) in quotes if r == "C"} & {k for (r, k) in quotes if r == "P"}
    if not strikes:
        return None, None
    k = min(strikes, key=lambda s: abs(s - price))
    dte = (ref_exp - today).days
    T = dte / 365
    ivs = []
    for r in ("C", "P"):
        bid, ask = quotes[(r, k)]
        if ask and ask > 0 and bid is not None and ask >= bid:
            v = implied_vol((bid + ask) / 2, price, k, T, r)
            if v:
                ivs.append(v)
    if not ivs:
        return None, None
    iv = sum(ivs) / len(ivs)
    return price * iv * math.sqrt(dte / 365) * EM_FACTOR, iv


def rule_put_strike(price: float, em: float, strikes):
    """Highest listed strike at or below price - EM."""
    below = [k for k in strikes if k <= price - em]
    return max(below) if below else None


def roll_strike(target: float, strikes):
    """Same strike if listed, else the closest listed one."""
    return min(strikes, key=lambda k: abs(k - target)) if strikes else None


def contracts_for(strike: float, cap: float, buying_power: float) -> int:
    n = int(min(cap, buying_power) // (strike * 100))
    return max(0, min(n, MAX_CONTRACTS))


def tick(px: float) -> float:
    return 0.01 if px < 3 else 0.05


def limit_price(bid: float, ask: float, side: str, step: float) -> float:
    """Price `step` of the way from mid to the far side, on a valid tick,
    rounded in the direction that makes it easier to fill."""
    mid = (bid + ask) / 2
    if side == "sell":
        px = mid - step * (mid - bid)
        t = tick(px)
        return max(t, math.floor(px / t + 1e-9) * t)
    px = mid + step * (ask - mid)
    t = tick(px)
    return math.ceil(px / t - 1e-9) * t


def is_last_trading_day_of_week(today: date, trading_days) -> bool:
    """trading_days: sorted dates covering today..at least next Monday."""
    later_this_week = [d for d in trading_days
                       if d > today and d.isocalendar()[:2] == today.isocalendar()[:2]]
    return today in trading_days and not later_this_week


# --------------------------------------------------------------------------- #
# Alpaca plumbing
# --------------------------------------------------------------------------- #
def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def log(event: dict) -> None:
    event = {"ts": datetime.now(timezone.utc).isoformat(), **event}
    with LOG_JSONL.open("a") as fh:
        fh.write(json.dumps(event, default=str) + "\n")
    is_new = not LOG_CSV.exists()
    with LOG_CSV.open("a", newline="") as fh:
        w = csv.writer(fh)
        if is_new:
            w.writerow(["timestamp", "action", "symbol", "qty", "price", "reason"])
        w.writerow([event["ts"], event.get("action", ""), event.get("symbol", ""),
                    event.get("qty", ""), event.get("price", ""), event.get("reason", "")])
    print(json.dumps(event, indent=2, default=str))


class Bot:
    def __init__(self, dry_run: bool):
        from alpaca.trading.client import TradingClient
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.historical.option import OptionHistoricalDataClient

        key, secret = os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY")
        if not key or not secret:
            print("Missing ALPACA_API_KEY / ALPACA_SECRET_KEY (env var or .env file next to this script).")
            sys.exit(1)
        self.dry_run = dry_run
        # paper=True is hard-coded on purpose.
        self.trade = TradingClient(key, secret, paper=True)
        self.stocks = StockHistoricalDataClient(key, secret)
        self.options = OptionHistoricalDataClient(key, secret)

    # ---- market data ------------------------------------------------------ #
    def price(self) -> float:
        from alpaca.data.requests import StockLatestQuoteRequest
        q = self.stocks.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=SYMBOL))[SYMBOL]
        if q.bid_price and q.ask_price:
            return (q.bid_price + q.ask_price) / 2
        from alpaca.data.requests import StockLatestTradeRequest
        return self.stocks.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=SYMBOL))[SYMBOL].price

    def chain(self, exp_from: date, exp_to: date) -> dict:
        """{expiration: {(right, strike): (bid, ask, occ_symbol)}}"""
        from alpaca.data.requests import OptionChainRequest
        snaps = self.options.get_option_chain(OptionChainRequest(
            underlying_symbol=SYMBOL, expiration_date_gte=exp_from, expiration_date_lte=exp_to))
        out: dict = {}
        for sym, s in snaps.items():
            p = parse_occ_symbol(sym)
            if p["underlying"] != SYMBOL or s.latest_quote is None:
                continue  # skips adjusted (non-standard) contracts like SOXL1
            q = s.latest_quote
            out.setdefault(p["expiration"], {})[(p["right"], p["strike"])] = (q.bid_price, q.ask_price, sym)
        return out

    def quote(self, occ: str):
        from alpaca.data.requests import OptionLatestQuoteRequest
        q = self.options.get_option_latest_quote(OptionLatestQuoteRequest(symbol_or_symbols=occ))[occ]
        return q.bid_price, q.ask_price

    # ---- account ---------------------------------------------------------- #
    def positions(self):
        """(share_qty, share_avg_price, [short option positions])"""
        shares, avg, shorts = 0, None, []
        for p in self.trade.get_all_positions():
            if p.symbol == SYMBOL:
                shares, avg = int(float(p.qty)), float(p.avg_entry_price)
            elif p.symbol.startswith(SYMBOL) and len(p.symbol) == len(SYMBOL) + 15 and float(p.qty) < 0:
                shorts.append({**parse_occ_symbol(p.symbol), "symbol": p.symbol, "qty": -int(float(p.qty))})
        return shares, avg, shorts

    def open_orders(self):
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        return [o for o in self.trade.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
                if o.symbol.startswith(SYMBOL)]

    def buying_power(self) -> float:
        a = self.trade.get_account()
        return float(a.options_buying_power or a.cash)

    def trading_days(self, start: date, end: date):
        from alpaca.trading.requests import GetCalendarRequest
        return sorted(c.date for c in self.trade.get_calendar(GetCalendarRequest(start=start, end=end)))

    # ---- orders ----------------------------------------------------------- #
    def work_order(self, occ: str, side: str, qty: int, reason: str) -> bool:
        """Limit order starting at mid, stepping toward bid/ask. True if filled."""
        from alpaca.trading.requests import LimitOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        if self.dry_run:
            bid, ask = self.quote(occ)
            log({"action": f"DRY_{side}", "symbol": occ, "qty": qty,
                 "price": limit_price(bid, ask, side, FILL_STEPS[-1]), "reason": f"{reason}; bid={bid} ask={ask}"})
            return True
        for step in FILL_STEPS:
            bid, ask = self.quote(occ)
            if not ask or ask <= 0 or (side == "sell" and not bid):
                log({"action": "alert", "symbol": occ, "reason": f"no usable quote bid={bid} ask={ask}"})
                return False
            px = round(limit_price(bid, ask, side, step), 2)
            o = self.trade.submit_order(LimitOrderRequest(
                symbol=occ, qty=qty, limit_price=px, time_in_force=TimeInForce.DAY,
                side=OrderSide.SELL if side == "sell" else OrderSide.BUY))
            deadline = time.time() + FILL_WAIT_SEC
            while time.time() < deadline:
                time.sleep(2)
                o = self.trade.get_order_by_id(o.id)
                if o.status.value == "filled":
                    log({"action": side, "symbol": occ, "qty": qty, "price": float(o.filled_avg_price),
                         "reason": f"{reason}; bid={bid} ask={ask} order_id={o.id}"})
                    return True
            self.trade.cancel_order_by_id(o.id)
            time.sleep(1)
            o = self.trade.get_order_by_id(o.id)
            filled = int(float(o.filled_qty or 0))
            if filled:  # partial fill: work only the remainder
                log({"action": f"{side}_partial", "symbol": occ, "qty": filled,
                     "price": float(o.filled_avg_price), "reason": reason})
                qty -= filled
                if qty <= 0:
                    return True
        log({"action": "alert", "symbol": occ, "qty": qty, "reason": f"{side} not filled after working the spread; {reason}"})
        return False

    def sell_shares(self, qty: int) -> bool:
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        if self.dry_run:
            log({"action": "DRY_sell_shares", "symbol": SYMBOL, "qty": qty, "reason": "unexpected assignment"})
            return True
        o = self.trade.submit_order(MarketOrderRequest(symbol=SYMBOL, qty=qty, side=OrderSide.SELL,
                                                       time_in_force=TimeInForce.DAY))
        for _ in range(15):
            time.sleep(2)
            o = self.trade.get_order_by_id(o.id)
            if o.status.value == "filled":
                log({"action": "sell_shares", "symbol": SYMBOL, "qty": qty, "price": float(o.filled_avg_price),
                     "reason": "assigned shares sold; rolling the put at the assignment price"})
                return True
        log({"action": "alert", "symbol": SYMBOL, "qty": qty, "reason": f"share sale order {o.id} not filled yet"})
        return False

    # ---- the strategy ----------------------------------------------------- #
    def run(self, force: bool) -> None:
        now = datetime.now(ET)
        today = now.date()
        days = self.trading_days(today - timedelta(days=1), today + timedelta(days=10))
        roll_day = is_last_trading_day_of_week(today, days)
        start, end = (datetime.combine(today, datetime.min.time(), ET).replace(hour=h, minute=m)
                      for h, m in TRADE_WINDOW_ET)
        in_window = start <= now <= end

        price = self.price()
        shares, avg, shorts = self.positions()
        orders = self.open_orders()
        pending = json.loads(PENDING_ROLL.read_text()) if PENDING_ROLL.exists() else None
        log({"action": "check", "symbol": SYMBOL, "price": round(price, 2),
             "reason": f"roll_day={roll_day} in_window={in_window} shares={shares} "
                       f"shorts={[s['symbol'] + 'x' + str(s['qty']) for s in shorts]} "
                       f"open_orders={len(orders)} pending_roll={pending}"})

        if orders:
            log({"action": "wait", "symbol": SYMBOL, "reason": "open SOXL order(s) exist; not stacking another"})
            return
        if not force and not (roll_day and in_window):
            if shares:
                log({"action": "alert", "symbol": SYMBOL, "qty": shares,
                     "reason": "holding SOXL shares (early assignment?); will sell and roll on the roll day"})
            return

        # 1) Shares from an unexpected assignment: sell them, roll at their price.
        roll_target, roll_qty = None, 0
        if pending:
            roll_target, roll_qty = pending["strike"], pending["contracts"]
        if shares > 0:
            if not self.sell_shares(shares):
                return
            roll_target, roll_qty = avg, shares // 100
        elif shares < 0:
            log({"action": "alert", "symbol": SYMBOL, "qty": shares, "reason": "short SOXL shares -- check manually"})
            return

        # 2) Expiring short puts: always buy back; in the money -> roll same strike.
        still_open = []
        for s in shorts:
            if s["right"] != "P":
                log({"action": "alert", "symbol": s["symbol"], "reason": "short call present -- not part of this strategy, check manually"})
                return
            if s["expiration"] > today:
                still_open.append(s)
                continue
            itm = price < s["strike"]
            if itm and not self.dry_run:
                PENDING_ROLL.write_text(json.dumps({"strike": s["strike"], "contracts": s["qty"], "date": str(today)}))
            if not self.work_order(s["symbol"], "buy", s["qty"],
                                   f"expiring put {'IN the money -> roll' if itm else 'out of the money -> close'}; price={price:.2f}"):
                return
            if itm:
                roll_target, roll_qty = s["strike"], s["qty"]
        if still_open:
            log({"action": "wait", "symbol": SYMBOL,
                 "reason": f"put(s) not expiring today: {[s['symbol'] for s in still_open]}"})
            return

        # 3) Open next week's put.
        ch = self.chain(today + timedelta(days=1), today + timedelta(days=REF_DTE[1] + 3))
        trade_exp, ref_exp = pick_expirations(ch.keys(), today)
        if trade_exp is None:
            log({"action": "alert", "symbol": SYMBOL, "reason": f"no expiration {TRADE_DTE[0]}-{TRADE_DTE[1]} days out"})
            return
        puts = {k: v for (r, k), v in ch[trade_exp].items() if r == "P" and v[0]}
        em = iv = None
        if roll_target is not None:
            strike = roll_strike(roll_target, list(puts))
            qty = roll_qty
            why = f"ROLL at strike {roll_target}"
        else:
            if ref_exp is None:
                log({"action": "alert", "symbol": SYMBOL, "reason": "no ~14 DTE reference expiration for the expected move"})
                return
            em, iv = expected_move(price, {rk: (v[0], v[1]) for rk, v in ch[ref_exp].items()}, ref_exp, today)
            if em is None:
                log({"action": "alert", "symbol": SYMBOL, "reason": "could not compute expected move (no ATM quotes)"})
                return
            strike = rule_put_strike(price, em, list(puts))
            qty = contracts_for(strike, ACCOUNT_CAP, self.buying_power()) if strike else 0
            why = f"NEW: price {price:.2f} - EM {em:.2f} (IV {iv:.1%}, ref exp {ref_exp}) = {price - em:.2f}"
        if not strike or qty <= 0:
            log({"action": "skip_put", "symbol": SYMBOL, "reason": f"{why}; strike={strike} contracts={qty} (cap/buying power)"})
            return
        bid, ask, occ = puts[strike]
        prem = bid * 100
        if self.work_order(occ, "sell", qty,
                           f"{why}; strike={strike} exp={trade_exp} bid=${prem:.0f}/contract "
                           f"({'meets' if prem >= PREMIUM_TARGET else 'below'} ${PREMIUM_TARGET:.0f} target)") \
                and not self.dry_run:
            PENDING_ROLL.unlink(missing_ok=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="log the plan, place no orders")
    ap.add_argument("--force", action="store_true", help="skip the roll-day and time-window checks")
    args = ap.parse_args()
    load_env_file(HERE / ".env")
    Bot(args.dry_run).run(args.force)


if __name__ == "__main__":
    main()
