"""
SPY weekly 10-delta short put -- scheduled paper-trading bot for Alpaca.

Trades the strategy backtested in ../spy_put_backtest (2013-2026 on real OPRA quotes),
with the 3x stop that came out best there and no VIX filter:

  Entry   First trading day of each week (Monday, or Tuesday after a Monday holiday).
          If that day is missed or the order doesn't fill, it retries on later days
          of the same week through Wednesday. Sells 1 cash-secured SPY put:
            - expiry: the listed one closest to 90 days out
            - strike: the put whose delta is closest to -0.10
            - DAY limit order at the mid minus ENTRY_CONCESSION (never below the bid)
  Exits   Checked every trading day, for each put this bot opened, whichever first:
            - profit target: mid <= 50% of the fill price  -> buy back (limit at the ask)
            - stop loss:     mid >= 3x the fill price      -> buy back (limit at ask + pad)
            - time exit:     21 days or fewer to expiry    -> buy back (limit at the ask)
          An exit order that doesn't fill is re-checked the next day.

Alpaca accounts can't sell naked options, so each put is cash-secured
(strike x 100 held as cash, ~$58k at SPY ~$650). MAX_CASH_SECURED caps the total
so other bots on the same account keep their cash.

Shares the account safely with other bots (e.g. nvda_condor_bot.py): it only reads,
manages and closes SPY options it opened itself, as recorded in its own log
(spy_put_log.jsonl -- keep that file), and never touches anything account-wide.

Paper trading only: paper=True is hard-coded. Run once per trading day ~3:45 PM ET.
First run:  python spy_put_bot.py --dry-run   (decides and logs, submits nothing)

Requires: pip install alpaca-py tzdata
Credentials: ALPACA_API_KEY / ALPACA_SECRET_KEY in a .env file next to this script
(only this folder's .env is read -- never another bot's).
"""

from __future__ import annotations

import csv
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# --------------------------------------------------------------------------- #
# Strategy parameters -- what was backtested. Change these and you are running
# a different, un-tested strategy.
# --------------------------------------------------------------------------- #
SYMBOL = "SPY"
CONTRACTS = 1                 # puts per weekly entry
TARGET_DTE = 90               # expiry closest to this many calendar days
DTE_SEARCH = (60, 120)        # look for expiries in this window
TARGET_DELTA = -0.10          # put delta closest to this
PROFIT_TARGET = 0.50          # buy back when the mid is <= 50% of the fill price
STOP_MULT = 3.0               # buy back when the mid is >= 3x the fill price
EXIT_DTE = 21                 # buy back at this many days to expiry
RETRY_THROUGH_WEEKDAY = 2     # a missed/unfilled entry is retried through Wednesday (Mon=0)

# Execution / safety (not part of the backtest's rules)
ENTRY_CONCESSION = 0.02       # sell limit = mid - this (never below the bid)
STOP_LIMIT_PAD = 0.10         # stop buy-back limit = ask + this, so it fills in a fast market
MAX_OPEN_POSITIONS = 13       # backtest peaked at 11 open at once
MAX_CASH_SECURED = 750_000.0  # cap on strike x 100 across this bot's open puts + new entry
MIN_BID = 0.05                # don't sell a put with no real bid
MAX_SPREAD = (0.50, 0.30)     # skip an exit when ask - bid > max($0.50, 30% of mid): likely a bad quote

HERE = Path(__file__).resolve().parent
LOG_JSONL = HERE / "spy_put_log.jsonl"
LOG_CSV = HERE / "spy_put_trades.csv"
ET = ZoneInfo("America/New_York")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def load_env_file(path: Path) -> None:
    """KEY=value lines from this folder's .env only."""
    if not path.exists():
        return
    raw = path.read_bytes()
    text = raw.decode("utf-16") if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else raw.decode("utf-8-sig")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def parse_occ(occ: str) -> dict:
    """SPY261218P00585000 -> underlying/expiration/right/strike."""
    i = 0
    while i < len(occ) and not occ[i].isdigit():
        i += 1
    rest = occ[i:]
    return {"underlying": occ[:i], "expiration": datetime.strptime(rest[:6], "%y%m%d").date(),
            "right": rest[6], "strike": int(rest[7:]) / 1000.0}


def is_spy_option(sym: str) -> bool:
    return sym.startswith(SYMBOL) and len(sym) > len(SYMBOL) + 6 and sym[len(SYMBOL)].isdigit()


def as_date(x) -> date:
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    return datetime.strptime(str(x)[:10], "%Y-%m-%d").date()


class Log:
    def __init__(self, jsonl=LOG_JSONL, csv_path=LOG_CSV, dry_run=False, echo=True):
        self.jsonl, self.csv, self.dry_run, self.echo = Path(jsonl), Path(csv_path), dry_run, echo

    def __call__(self, event: dict) -> dict:
        event = {"ts": datetime.now(timezone.utc).isoformat(), "dry_run": self.dry_run, **event}
        with self.jsonl.open("a") as fh:
            fh.write(json.dumps(event, default=str) + "\n")
        is_new = not self.csv.exists()
        with self.csv.open("a", newline="") as fh:
            w = csv.writer(fh)
            if is_new:
                w.writerow(["timestamp", "dry_run", "action", "symbol", "qty", "limit", "reason"])
            w.writerow([event["ts"], self.dry_run, event.get("action", ""), event.get("symbol", ""),
                        event.get("qty", ""), event.get("limit", ""), event.get("reason", "")])
        if self.echo:
            print(json.dumps(event, indent=2, default=str))
        return event

    def events(self, action: str) -> list[dict]:
        """Real (not dry-run) events of one kind, oldest first."""
        if not self.jsonl.exists():
            return []
        out = []
        for line in self.jsonl.read_text().splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("action") == action and not e.get("dry_run"):
                out.append(e)
        return out


# --------------------------------------------------------------------------- #
# Alpaca access -- everything the bot asks of the broker goes through here
# --------------------------------------------------------------------------- #
class AlpacaBroker:
    def __init__(self, key: str, secret: str):
        from alpaca.data.enums import DataFeed
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.historical.option import OptionHistoricalDataClient
        from alpaca.trading.client import TradingClient

        # paper=True is hard-coded on purpose -- this script is not the place a live
        # decision gets made.
        self.trade = TradingClient(key, secret, paper=True)
        self.stocks = StockHistoricalDataClient(key, secret)
        self.options = OptionHistoricalDataClient(key, secret)
        self.feed = DataFeed.IEX      # free data plan; DataFeed.SIP if you pay for it

    def trading_days(self, start: date, end: date) -> list[date]:
        from alpaca.trading.requests import GetCalendarRequest
        cal = self.trade.get_calendar(GetCalendarRequest(start=start, end=end))
        return sorted(as_date(c.date) for c in cal)

    def last_price(self) -> float:
        from alpaca.data.requests import StockLatestTradeRequest
        t = self.stocks.get_stock_latest_trade(
            StockLatestTradeRequest(symbol_or_symbols=SYMBOL, feed=self.feed))[SYMBOL]
        return float(t.price)

    def positions(self) -> list[dict]:
        out = []
        for p in self.trade.get_all_positions():
            qty = abs(int(float(p.qty)))
            short = str(getattr(p.side, "value", p.side)).lower() == "short" or float(p.qty) < 0
            out.append(dict(symbol=p.symbol, qty=-qty if short else qty, avg_entry_price=abs(float(p.avg_entry_price))))
        return out

    def open_orders(self) -> list[dict]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        orders = self.trade.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=True))
        out = []
        for o in orders:
            syms = [o.symbol or ""] + [leg.symbol for leg in (o.legs or [])]
            for s in syms:
                if s:
                    out.append(dict(id=str(o.id), symbol=s, side=getattr(o.side, "value", o.side)))
        return out

    def order(self, oid: str) -> dict | None:
        try:
            o = self.trade.get_order_by_id(oid)
        except Exception:  # noqa: BLE001
            return None
        return dict(status=getattr(o.status, "value", o.status), filled_qty=float(o.filled_qty or 0),
                    filled_avg_price=float(o.filled_avg_price) if o.filled_avg_price else None)

    def put_expirations(self, first: date, last: date) -> list[date]:
        from alpaca.trading.enums import AssetStatus, ContractType
        from alpaca.trading.requests import GetOptionContractsRequest
        req = GetOptionContractsRequest(underlying_symbols=[SYMBOL], status=AssetStatus.ACTIVE,
                                        type=ContractType.PUT, expiration_date_gte=first.isoformat(),
                                        expiration_date_lte=last.isoformat(), limit=10000)
        exps = set()
        while True:
            resp = self.trade.get_option_contracts(req)
            for c in resp.option_contracts or []:
                exps.add(as_date(c.expiration_date))
            if not resp.next_page_token:
                break
            req.page_token = resp.next_page_token
        return sorted(exps)

    def put_chain(self, exp: date, k_lo: float, k_hi: float) -> list[dict]:
        """[{symbol, strike, bid, ask, mid, delta}] for one expiry, by strike."""
        from alpaca.data.requests import OptionChainRequest
        from alpaca.trading.enums import ContractType
        snaps = self.options.get_option_chain(OptionChainRequest(
            underlying_symbol=SYMBOL, type=ContractType.PUT, expiration_date=exp,
            strike_price_gte=k_lo, strike_price_lte=k_hi))
        rows = []
        for sym, s in snaps.items():
            q = s.latest_quote
            if q is None or not q.ask_price:
                continue
            delta = s.greeks.delta if s.greeks is not None else None
            bid, ask = float(q.bid_price or 0), float(q.ask_price)
            rows.append(dict(symbol=sym, strike=parse_occ(sym)["strike"], bid=bid, ask=ask,
                             mid=(bid + ask) / 2, delta=None if delta is None else float(delta)))
        return sorted(rows, key=lambda r: r["strike"])

    def quotes(self, symbols: list[str]) -> dict[str, tuple[float, float]]:
        from alpaca.data.requests import OptionLatestQuoteRequest
        if not symbols:
            return {}
        qs = self.options.get_option_latest_quote(OptionLatestQuoteRequest(symbol_or_symbols=symbols))
        return {s: (float(q.bid_price or 0), float(q.ask_price or 0)) for s, q in qs.items()}

    def submit_limit(self, symbol: str, side: str, qty: int, limit: float, opening: bool) -> str:
        from alpaca.trading.enums import OrderSide, PositionIntent, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest
        intent = {("sell", True): PositionIntent.SELL_TO_OPEN, ("buy", False): PositionIntent.BUY_TO_CLOSE}[
            (side, opening)]
        req = LimitOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL if side == "sell" else OrderSide.BUY,
                                time_in_force=TimeInForce.DAY, limit_price=round(limit, 2),
                                position_intent=intent)
        return str(self.trade.submit_order(req).id)


# --------------------------------------------------------------------------- #
# strategy
# --------------------------------------------------------------------------- #
def entry_filled(broker, event: dict) -> bool:
    """Did a logged entry order fill (even partly) or is it still working? If it can't be
    checked, assume yes -- the safe side: the bot then waits instead of selling a second put."""
    oid = event.get("order_id")
    if not oid or oid == "DRY-RUN":
        return True
    o = broker.order(oid)
    if o is None:
        return True
    if o["filled_qty"] > 0:
        return True
    return o["status"] not in ("canceled", "expired", "rejected", "done_for_day")


def decide_exit(credit: float, bid: float, ask: float, dte: int) -> tuple[str, float] | None:
    """(reason, limit) to buy back, or None to hold. Same order as the backtest:
    profit target, then stop, then time."""
    if ask <= 0:
        return None
    mid = (max(bid, 0.0) + ask) / 2
    if mid <= PROFIT_TARGET * credit:
        return "profit_target", ask
    if mid >= STOP_MULT * credit:
        return "stop_loss", ask + STOP_LIMIT_PAD
    if dte <= EXIT_DTE:
        return "time_21dte", ask
    return None


def pick_put(broker, today: date, price: float):
    """(expiration, row) for the put closest to TARGET_DELTA at the expiry closest to
    TARGET_DTE, or (None, reason)."""
    exps = broker.put_expirations(today + timedelta(days=DTE_SEARCH[0]), today + timedelta(days=DTE_SEARCH[1]))
    if not exps:
        return None, f"no SPY put expirations {DTE_SEARCH[0]}-{DTE_SEARCH[1]} days out"
    exp = min(exps, key=lambda e: (abs((e - today).days - TARGET_DTE), e))
    rows = [r for r in broker.put_chain(exp, price * 0.60, price * 1.0)
            if r["delta"] is not None and r["delta"] < 0 and r["bid"] >= MIN_BID]
    if not rows:
        return None, f"no quoted puts with greeks for {exp}"
    return exp, min(rows, key=lambda r: abs(r["delta"] - TARGET_DELTA))


def run(broker, log: Log, today: date, dry_run: bool = False) -> None:
    week_start = today - timedelta(days=today.weekday())
    days = broker.trading_days(week_start, week_start + timedelta(days=6))
    if today not in days:
        log({"action": "wait", "reason": f"{today} is not a trading day"})
        return
    now_et = datetime.now(ET)
    if not (15 <= now_et.hour < 16) and not dry_run:
        log({"action": "note", "reason": f"running at {now_et:%H:%M} ET; the backtest traded at the close "
                                         "(schedule ~3:45 PM ET)"})

    opens = log.events("open_put")
    mine = {e["symbol"] for e in opens}
    held = [p for p in broker.positions() if p["symbol"] in mine and p["qty"] < 0]
    foreign = sorted(p["symbol"] for p in broker.positions() if p["symbol"] not in mine)
    pending = [o for o in broker.open_orders() if o["symbol"] in mine]
    pending_syms = {o["symbol"] for o in pending}
    price = broker.last_price()
    log({"action": "check", "reason": f"SPY {price:.2f}; holding {len(held)} of this bot's puts; "
                                      f"{len(pending)} open order(s); other positions left alone: {len(foreign)}"})

    # ---- 1) exits
    quotes = broker.quotes([p["symbol"] for p in held if p["symbol"] not in pending_syms])
    for p in held:
        sym = p["symbol"]
        if sym in pending_syms:
            log({"action": "wait", "symbol": sym, "reason": "order already working on this put"})
            continue
        exp = parse_occ(sym)["expiration"]
        dte = (exp - today).days
        bid, ask = quotes.get(sym, (0.0, 0.0))
        credit = p["avg_entry_price"]
        if ask <= 0:
            log({"action": "alert", "symbol": sym, "reason": f"no quote today ({dte} DTE); will re-check next run"})
            continue
        mid = (bid + ask) / 2
        if bid <= 0 or ask - bid > max(MAX_SPREAD[0], MAX_SPREAD[1] * mid):
            log({"action": "alert", "symbol": sym, "reason": f"quote {bid:.2f}/{ask:.2f} looks bad (no bid or too wide); "
                 f"no exit decision on it, will re-check next run ({dte} DTE)"})
            continue
        decision = decide_exit(credit, bid, ask, dte)
        if decision is None:
            log({"action": "hold", "symbol": sym, "reason": f"mid {mid:.2f} vs credit {credit:.2f} "
                 f"(target {PROFIT_TARGET * credit:.2f}, stop {STOP_MULT * credit:.2f}); {dte} DTE"})
            continue
        reason, limit = decision
        qty = abs(p["qty"])
        oid = "DRY-RUN" if dry_run else broker.submit_limit(sym, "buy", qty, limit, opening=False)
        log({"action": "close_put", "symbol": sym, "qty": qty, "limit": round(limit, 2), "order_id": oid,
             "exit_reason": reason, "credit": credit, "mid": round(mid, 3), "dte": dte,
             "reason": f"{reason}: mid {mid:.2f} vs credit {credit:.2f}, {dte} DTE; buy limit {limit:.2f}"})

    # ---- 2) this week's entry
    this_week = [e for e in opens if week_start <= as_date(e.get("trade_date") or e["ts"][:10]) <= today]
    done = [e for e in this_week if entry_filled(broker, e)]
    if done:
        log({"action": "wait", "reason": f"this week's put already sold or working ({done[-1]['symbol']})"})
        return
    if today < days[0] or today.weekday() > RETRY_THROUGH_WEEKDAY:
        log({"action": "wait", "reason": "no entry this week and it's past Wednesday; next entry next week"})
        return
    if len(held) + 1 > MAX_OPEN_POSITIONS:
        log({"action": "skip_entry", "reason": f"{len(held)} puts open; cap is {MAX_OPEN_POSITIONS}"})
        return
    secured = sum(parse_occ(p["symbol"])["strike"] * 100 * abs(p["qty"]) for p in held)
    exp, row = pick_put(broker, today, price)
    if exp is None:
        log({"action": "skip_entry", "reason": row})
        return
    need = row["strike"] * 100 * CONTRACTS
    if secured + need > MAX_CASH_SECURED:
        log({"action": "skip_entry", "symbol": row["symbol"],
             "reason": f"cash secured would be ${secured + need:,.0f} > cap ${MAX_CASH_SECURED:,.0f}"})
        return
    limit = max(row["bid"], round(row["mid"] - ENTRY_CONCESSION, 2))
    oid = "DRY-RUN" if dry_run else broker.submit_limit(row["symbol"], "sell", CONTRACTS, limit, opening=True)
    retry = "" if today == days[0] else " (retry: first day of the week missed or unfilled)"
    log({"action": "open_put", "trade_date": today.isoformat(), "symbol": row["symbol"], "qty": CONTRACTS,
         "limit": limit, "order_id": oid,
         "expiration": exp.isoformat(), "dte": (exp - today).days, "strike": row["strike"],
         "delta": row["delta"], "bid": row["bid"], "ask": row["ask"], "underlying_price": price,
         "cash_secured_after": secured + need,
         "reason": f"{(exp - today).days} DTE, strike {row['strike']:g}, delta {row['delta']:.3f}, "
                   f"bid/ask {row['bid']:.2f}/{row['ask']:.2f}, sell limit {limit:.2f}{retry}"})


def main():
    dry_run = "--dry-run" in sys.argv
    load_env_file(HERE / ".env")
    key, secret = os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        sys.exit(f"Missing ALPACA_API_KEY / ALPACA_SECRET_KEY. Put them in {HERE / '.env'} "
                 "(the paper keys of the account this bot should trade).")
    run(AlpacaBroker(key, secret), Log(dry_run=dry_run), datetime.now(ET).date(), dry_run=dry_run)


if __name__ == "__main__":
    main()
