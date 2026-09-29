"""
Bollinger oversold put credit spreads -- scheduled paper-trading bot for Alpaca.

Same account, same style as the NVDA delayed iron condor bot: paper=True is hard-coded,
every decision is logged, and the bot only ever touches option contracts it opened
itself (recorded in bb_state.json), so it can share the paper account with the condor
bot and the other bots.

Rules (bb_rules.py holds the pure logic):
  Current settings = backtest variant A+B+D (see README); the original rules are one
  config switch away (ENTRY_SIGNAL, STRIKE_OFFSET, DTE_MIN/DTE_MAX, MONTHLY_ONLY).
  SIGNAL  (run after the close, --signal)
    Daily closes, Bollinger 50-day SMA +/- 2 sd.
      A (ENTRY_SIGNAL="reclaim"): today's close is back at/above the lower band after
        yesterday's close below it.   ("cross_below": today's close crosses below it.)
    The signal, the band and the 6-month volume-profile POC are saved for the next morning.
  ENTRY   (run the next trading morning, --enter)
    Expiration: D -- 30-60 DTE, weeklies allowed (MONTHLY_ONLY=False), expiring before the
    next earnings date (Alpha Vantage EARNINGS_CALENDAR); nearest one wins; none -> skip.
    Short put: B -- highest listed strike below BOTH the POC and the lower band, each
    lowered by STRIKE_OFFSET (5%).
    Long put = the listed strike nearest (short - 1% of the price), at least one strike down.
    One multi-leg DAY limit order at the mid credit, 1 contract.
    Skip if the mid credit < $0.50. Max 5 open spreads, 2 per sector, 1 per ticker.
    Optionally leaves a resting GTC buy-to-close at 50% of the credit once filled.
  EXITS   (run ~3:45 PM ET, --manage), using mid prices:
    stop-loss       spread value >= 2x entry credit
    backup stop     underlying (latest price near the close) below the short strike
    take profit     spread value <= 50% of entry credit
    time stop       21 DTE or less

Every signal (taken or skipped, with the reason) goes to bb_signals.csv; every closed
trade (dates, strikes, credit, exit price, P&L, exit reason) goes to bb_trades.csv;
everything also goes to bb_log.jsonl.

Usage (Windows Task Scheduler, weekdays):
  python bb_put_spread_bot.py --signal     ~4:30 PM ET  (after the close)
  python bb_put_spread_bot.py --enter      ~9:45 AM ET  (next morning)
  python bb_put_spread_bot.py --manage     ~3:45 PM ET  (near the close)
Add --dry-run to decide and log without submitting orders or saving state.
  python bb_put_spread_bot.py --enter --dry-run --force-entry AMD
    runs the full entry pipeline for AMD today as if it had signalled (dry run only).
  python bb_put_spread_bot.py --enter --dry-run --force-entry AMD --ignore-earnings
    same, with the earnings filter switched off, to see the strikes and credit it would
    pick (test only; refused without --dry-run).

Requires: pip install alpaca-py
Credentials: ALPACA_API_KEY / ALPACA_SECRET_KEY / ALPHAVANTAGE_API_KEY env vars or a
.env file next to this script.
"""

from __future__ import annotations

import csv
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, date, timezone
from pathlib import Path

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    GetCalendarRequest,
    GetOptionContractsRequest,
    LimitOrderRequest,
    OptionLegRequest,
)
from alpaca.trading.enums import (
    AssetStatus,
    ContractType,
    OrderClass,
    OrderSide,
    PositionIntent,
    TimeInForce,
)
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionSnapshotRequest, StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import Adjustment, DataFeed

import bb_rules as rules

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
# ticker -> sector (sector drives the max-2-per-sector rule). Add tickers here.
WATCHLIST = {
    "AMD": "Technology",
}
QTY = 1                       # contracts per trade
MAX_OPEN_TOTAL = 5
MAX_PER_SECTOR = 2
MAX_PER_TICKER = 1
MIN_CREDIT = 0.50             # skip if the mid credit is below this
# Backtest variant A+B+D (bb_stock_backtest.py). Original rules: "cross_below", 0.0, 45, 90, True.
ENTRY_SIGNAL = "reclaim"      # A: close back above the lower band ("cross_below" = first close below it)
STRIKE_OFFSET = 0.05          # B: short strike below min(POC, lower band) x (1 - 5%)
DTE_MIN, DTE_MAX = 30, 60     # D: 30-60 DTE ...
MONTHLY_ONLY = False          # D: ... weekly expirations allowed
RESTING_TP_ORDER = True       # leave a GTC buy-to-close at 50% of the credit after the fill
EARNINGS_HORIZON = "6month"   # Alpha Vantage EARNINGS_CALENDAR horizon (covers 90 DTE)
BAR_DAYS = 300                # calendar days of daily bars fetched (>= 126 trading days)

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "bb_state.json"
LOG_JSONL = HERE / "bb_log.jsonl"
SIGNALS_CSV = HERE / "bb_signals.csv"
TRADES_CSV = HERE / "bb_trades.csv"
EARNINGS_CACHE = HERE / "bb_earnings_cache.json"

DRY_RUN = "--dry-run" in sys.argv
# Test-only: skip the earnings filter to see which strikes/credit the bot would pick.
# Refused without --dry-run, so it can never affect a real order.
IGNORE_EARNINGS = "--ignore-earnings" in sys.argv
if IGNORE_EARNINGS and not DRY_RUN:
    print("--ignore-earnings only runs with --dry-run")
    sys.exit(1)


def load_env_file(path: Path) -> None:
    """Minimal .env loader so this script has no extra dependency for it. Tolerates what
    Windows editors produce: a UTF-8 BOM, UTF-16 (PowerShell 5 redirection), quotes,
    'export ' prefixes and spaces around '='."""
    if not path.exists():
        return
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16")
    else:
        text = raw.decode("utf-8-sig", errors="replace")
    for line in text.splitlines():
        line = line.strip().lstrip("﻿")
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


ENV_CANDIDATES = [HERE / ".env", HERE / ".env.txt", HERE / "env.txt", HERE / "env"]
for _p in ENV_CANDIDATES:                  # Notepad often saves ".env" as ".env.txt"
    load_env_file(_p)

API_KEY = os.environ.get("ALPACA_API_KEY")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
AV_KEY = os.environ.get("ALPHAVANTAGE_API_KEY")
if not API_KEY or not SECRET_KEY:
    found = [p.name for p in ENV_CANDIDATES if p.exists()]
    print("Missing ALPACA_API_KEY / ALPACA_SECRET_KEY (env var or .env file next to this script).")
    print(f"  Looked in: {HERE}")
    print(f"  Env files found there: {found or 'none'}")
    print(f"  Files in that folder: {sorted(p.name for p in HERE.iterdir())}")
    print("  The file must contain lines exactly like:  ALPACA_API_KEY=PK...")
    sys.exit(1)

# paper=True is hard-coded here on purpose -- this script is not the place a
# live decision gets made.
trade_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
stock_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)
option_client = OptionHistoricalDataClient(API_KEY, SECRET_KEY)


# --------------------------------------------------------------------------- #
# state + logging
# --------------------------------------------------------------------------- #
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"pending_signals": [], "spreads": []}


def save_state(state: dict) -> None:
    if DRY_RUN:
        return
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, default=str))
    tmp.replace(STATE_FILE)


def log(event: dict) -> None:
    event = {"ts": datetime.now(timezone.utc).isoformat(), "dry_run": DRY_RUN, **event}
    with LOG_JSONL.open("a") as fh:
        fh.write(json.dumps(event, default=str) + "\n")
    print(json.dumps(event, default=str))


def append_csv(path: Path, header: list[str], row: dict) -> None:
    is_new = not path.exists()
    with path.open("a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=header, extrasaction="ignore")
        if is_new:
            w.writeheader()
        w.writerow(row)


SIGNAL_FIELDS = ["logged_at", "dry_run", "signal_date", "ticker", "sector", "close", "lower_band",
                 "sma", "poc", "outcome", "reason", "expiration", "short_strike", "long_strike",
                 "width", "mid_credit", "order_id"]
TRADE_FIELDS = ["ticker", "sector", "signal_date", "entry_date", "exit_date", "expiration",
                "short_strike", "long_strike", "width", "qty", "entry_credit", "exit_price", "pnl",
                "exit_reason", "entry_order_id", "exit_order_id"]


def record_signal(sig: dict, outcome: str, reason: str, **extra) -> None:
    row = {"logged_at": datetime.now(timezone.utc).isoformat(), "dry_run": DRY_RUN,
           "signal_date": sig["signal_date"], "ticker": sig["ticker"], "sector": sig["sector"],
           "close": round(sig["close"], 2), "lower_band": round(sig["lower"], 2),
           "sma": round(sig["sma"], 2), "poc": round(sig["poc"], 2),
           "outcome": outcome, "reason": reason, **extra}
    append_csv(SIGNALS_CSV, SIGNAL_FIELDS, row)
    log({"action": f"signal_{outcome}", **row})


def record_trade(sp: dict, exit_price: float, exit_reason: str, exit_order_id) -> None:
    pnl = round((sp["entry_credit"] - exit_price) * 100 * sp["qty"], 2)
    row = {**sp, "exit_date": date.today().isoformat(), "exit_price": round(exit_price, 2),
           "pnl": pnl, "exit_reason": exit_reason, "exit_order_id": exit_order_id}
    if not DRY_RUN:
        append_csv(TRADES_CSV, TRADE_FIELDS, row)
    log({"action": "trade_closed", **{k: row.get(k) for k in TRADE_FIELDS}})


# --------------------------------------------------------------------------- #
# market data
# --------------------------------------------------------------------------- #
def as_date(x) -> date:
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    return date.fromisoformat(str(x)[:10])


def daily_bars(ticker: str) -> tuple[list[dict], str]:
    """Split-adjusted daily bars, oldest first. SIP (full consolidated volume, free on
    Alpaca when the request ends >15 min ago); falls back to IEX if SIP is refused."""
    end = datetime.now(timezone.utc) - timedelta(minutes=16)
    start = end - timedelta(days=BAR_DAYS)
    for feed in (DataFeed.SIP, DataFeed.IEX):
        try:
            req = StockBarsRequest(symbol_or_symbols=ticker, timeframe=TimeFrame.Day, start=start,
                                   end=end, adjustment=Adjustment.ALL, feed=feed)
            bars = stock_client.get_stock_bars(req)[ticker]
            return [dict(d=as_date(b.timestamp), o=float(b.open), h=float(b.high), l=float(b.low),
                         c=float(b.close), v=float(b.volume)) for b in bars], feed.value
        except Exception as exc:                          # noqa: BLE001
            last_err = exc
    raise RuntimeError(f"no daily bars for {ticker}: {last_err}")


def last_price(ticker: str) -> float:
    t = stock_client.get_stock_latest_trade(
        StockLatestTradeRequest(symbol_or_symbols=ticker, feed=DataFeed.IEX))[ticker]
    return float(t.price)


def trading_days(start: date, end: date) -> list[date]:
    cal = trade_client.get_calendar(GetCalendarRequest(start=start, end=end))
    return sorted(as_date(c.date) for c in cal)


def previous_trading_day(today: date) -> date | None:
    days = [d for d in trading_days(today - timedelta(days=10), today) if d < today]
    return days[-1] if days else None


def list_contracts(ticker: str, **filters) -> list:
    req = GetOptionContractsRequest(underlying_symbols=[ticker], status=AssetStatus.ACTIVE,
                                    type=ContractType.PUT, limit=10000, **filters)
    out = []
    while True:
        resp = trade_client.get_option_contracts(req)
        out.extend(resp.option_contracts or [])
        if not resp.next_page_token:
            return out
        req.page_token = resp.next_page_token


def mids(symbols: list[str]) -> dict[str, dict]:
    """{symbol: {bid, ask, mid}} for symbols with a two-sided quote."""
    snaps = option_client.get_option_snapshot(OptionSnapshotRequest(symbol_or_symbols=symbols))
    out = {}
    for sym, s in snaps.items():
        q = getattr(s, "latest_quote", None)
        if q is None or not q.bid_price or not q.ask_price:
            continue
        bid, ask = float(q.bid_price), float(q.ask_price)
        out[sym] = {"bid": bid, "ask": ask, "mid": (bid + ask) / 2}
    return out


def next_earnings(ticker: str) -> tuple[bool, date | None, str]:
    """(ok, next report date or None, note). Cached for the day to spare the free-tier quota."""
    today = date.today()
    cache = json.loads(EARNINGS_CACHE.read_text()) if EARNINGS_CACHE.exists() else {}
    hit = cache.get(ticker)
    if hit and hit.get("fetched") == today.isoformat():
        nxt = hit.get("next")
        return True, (date.fromisoformat(nxt) if nxt else None), "cached"
    if not AV_KEY:
        return False, None, "ALPHAVANTAGE_API_KEY not set"
    url = "https://www.alphavantage.co/query?" + urllib.parse.urlencode(
        {"function": "EARNINGS_CALENDAR", "symbol": ticker, "horizon": EARNINGS_HORIZON, "apikey": AV_KEY})
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            text = r.read().decode("utf-8", "replace")
    except Exception as exc:                              # noqa: BLE001
        return False, None, f"Alpha Vantage request failed: {exc}"
    ok, nxt = rules.parse_earnings_csv(text, ticker, today)
    if not ok:
        return False, None, f"unexpected Alpha Vantage response: {text[:150]!r}"
    cache[ticker] = {"fetched": today.isoformat(), "next": nxt.isoformat() if nxt else None}
    EARNINGS_CACHE.write_text(json.dumps(cache, indent=2))
    return True, nxt, "fetched"


# --------------------------------------------------------------------------- #
# orders
# --------------------------------------------------------------------------- #
def submit_spread(legs: list[tuple[str, PositionIntent]], qty: int, price: float, credit: bool,
                  tif: TimeInForce = TimeInForce.DAY):
    """Multi-leg limit order. Alpaca mleg convention (confirmed on the condor bot's first
    paper order): limit_price NEGATIVE = net credit, POSITIVE = net debit."""
    leg_reqs = [OptionLegRequest(symbol=s, ratio_qty=1, position_intent=pi,
                                 side=OrderSide.SELL if pi in (PositionIntent.SELL_TO_OPEN,
                                                               PositionIntent.SELL_TO_CLOSE) else OrderSide.BUY)
                for s, pi in legs]
    px = round(abs(price), 2)
    req = LimitOrderRequest(qty=qty, order_class=OrderClass.MLEG, legs=leg_reqs, time_in_force=tif,
                            limit_price=-px if credit else px)
    if DRY_RUN:
        return "DRY-RUN"
    return str(trade_client.submit_order(req).id)


def order_info(oid: str) -> tuple[str, float, float | None]:
    """(status, filled_qty, abs(filled_avg_price) or None)."""
    o = trade_client.get_order_by_id(oid)
    status = getattr(o.status, "value", o.status)
    fap = getattr(o, "filled_avg_price", None)
    return str(status), float(o.filled_qty or 0), (abs(float(fap)) if fap not in (None, "") else None)


TERMINAL = {"canceled", "expired", "rejected", "done_for_day", "replaced"}


def cancel_and_confirm(oid: str) -> tuple[str, float, float | None]:
    """Cancel an order and wait (briefly) for Alpaca to confirm; returns its final info."""
    try:
        trade_client.cancel_order_by_id(oid)
    except Exception:                                     # noqa: BLE001 -- may already be filled/closed
        pass
    for _ in range(10):
        info = order_info(oid)
        if info[0] in TERMINAL or info[0] == "filled":
            return info
        time.sleep(1)
    return order_info(oid)


def place_resting_tp(sp: dict) -> None:
    if not RESTING_TP_ORDER:
        return
    target = max(0.01, round(rules.TAKE_PROFIT_FRAC * sp["entry_credit"], 2))
    try:
        sp["tp_order_id"] = submit_spread([(sp["short_symbol"], PositionIntent.BUY_TO_CLOSE),
                                           (sp["long_symbol"], PositionIntent.SELL_TO_CLOSE)],
                                          sp["qty"], target, credit=False, tif=TimeInForce.GTC)
        log({"action": "resting_tp_placed", "ticker": sp["ticker"], "debit": target, "order_id": sp["tp_order_id"]})
    except Exception as exc:                              # noqa: BLE001
        sp["tp_order_id"] = None
        log({"action": "alert", "ticker": sp["ticker"],
             "reason": f"resting GTC take-profit rejected ({exc}); the daily --manage check still takes profit"})


# --------------------------------------------------------------------------- #
# reconciliation (runs at the start of every mode)
# --------------------------------------------------------------------------- #
def reconcile(state: dict) -> None:
    """Bring each tracked spread up to date with its orders on Alpaca."""
    keep = []
    held = {p.symbol: float(p.qty) for p in trade_client.get_all_positions()}
    for sp in state["spreads"]:
        st = sp["status"]
        if st == "opening":
            status, filled, fap = order_info(sp["entry_order_id"])
            if status == "filled" or (status in TERMINAL and filled > 0):
                sp.update(status="open", qty=int(filled), entry_credit=fap or sp["limit_credit"])
                log({"action": "entry_filled", "ticker": sp["ticker"], "credit": sp["entry_credit"],
                     "legs": f"{sp['short_symbol']}/{sp['long_symbol']}", "qty": sp["qty"]})
                place_resting_tp(sp)
            elif status in TERMINAL:
                record_signal(sp["signal"], "entry_not_filled",
                              f"entry order {sp['entry_order_id']} {status} with no fill",
                              expiration=sp["expiration"], short_strike=sp["short_strike"],
                              long_strike=sp["long_strike"], mid_credit=sp["limit_credit"],
                              order_id=sp["entry_order_id"])
                continue                                  # drop it: the signal is spent
        elif st == "open" and sp.get("tp_order_id"):
            status, filled, fap = order_info(sp["tp_order_id"])
            if status == "filled":
                record_trade(sp, fap if fap is not None else rules.TAKE_PROFIT_FRAC * sp["entry_credit"],
                             "take_profit (resting GTC)", sp["tp_order_id"])
                continue
            if status in TERMINAL:
                log({"action": "alert", "ticker": sp["ticker"],
                     "reason": f"resting take-profit order {status}; relying on the daily check"})
                sp["tp_order_id"] = None
        elif st == "closing":
            status, filled, fap = order_info(sp["close_order_id"])
            if status == "filled":
                record_trade(sp, fap if fap is not None else sp["close_limit"], sp["exit_reason"],
                             sp["close_order_id"])
                continue
            if status in TERMINAL:
                log({"action": "close_not_filled", "ticker": sp["ticker"], "reason":
                     f"{sp['exit_reason']} close order {status}; will retry at the next --manage run"})
                sp.update(status="open", close_order_id=None)
        if sp["status"] == "open" and sp["short_symbol"] not in held and sp["long_symbol"] not in held:
            log({"action": "alert", "ticker": sp["ticker"], "reason":
                 f"legs {sp['short_symbol']}/{sp['long_symbol']} no longer on the account (closed by hand, "
                 "expired or assigned?) -- dropped from tracking; not recorded as a trade"})
            continue
        keep.append(sp)
    state["spreads"] = keep


# --------------------------------------------------------------------------- #
# modes
# --------------------------------------------------------------------------- #
def run_signal(state: dict) -> None:
    today = date.today()
    if today not in trading_days(today, today):
        log({"action": "no_session", "reason": f"{today} is not a trading day"})
        return
    for ticker, sector in WATCHLIST.items():
        bars, feed = daily_bars(ticker)
        if not bars or bars[-1]["d"] != today:
            log({"action": "alert", "ticker": ticker, "reason": f"no completed daily bar for {today} yet "
                 f"(last {bars[-1]['d'] if bars else None}) -- run --signal after the close"})
            continue
        if len(bars) < rules.POC_LOOKBACK:
            log({"action": "alert", "ticker": ticker, "reason": f"only {len(bars)} daily bars"})
            continue
        hit = (rules.cross_above_lower if ENTRY_SIGNAL == "reclaim" else rules.cross_below_lower)(bars)
        closes = [b["c"] for b in bars]
        sma, lower, _ = rules.bollinger(closes, len(bars) - 1)
        if not hit:
            log({"action": "no_signal", "ticker": ticker, "feed": feed, "close": closes[-1],
                 "lower_band": round(lower, 2), "sma": round(sma, 2)})
            continue
        sig = {"ticker": ticker, "sector": sector, "signal_date": today.isoformat(),
               "close": hit["close"], "lower": hit["lower"], "sma": hit["sma"],
               "poc": rules.volume_profile_poc(bars), "feed": feed}
        book = [s for s in state["spreads"]] + [p for p in state["pending_signals"] if p["ticker"] != ticker]
        why = rules.limit_violation(ticker, sector, book, MAX_OPEN_TOTAL, MAX_PER_SECTOR, MAX_PER_TICKER)
        if why:
            record_signal(sig, "skipped", why)
            continue
        state["pending_signals"] = [p for p in state["pending_signals"] if p["ticker"] != ticker] + [sig]
        what = ("close back above the lower band after an oversold close" if ENTRY_SIGNAL == "reclaim"
                else "close crossed below the lower band")
        record_signal(sig, "pending", f"{what}; order goes in next trading morning")


def run_enter(state: dict) -> None:
    today = date.today()
    if today not in trading_days(today, today):
        log({"action": "no_session", "reason": f"{today} is not a trading day"})
        return
    prev = previous_trading_day(today)
    pending, state["pending_signals"] = state["pending_signals"], []

    force = sys.argv[sys.argv.index("--force-entry") + 1].upper() if "--force-entry" in sys.argv else None
    if force:
        if not DRY_RUN:
            print("--force-entry only runs with --dry-run")
            sys.exit(1)
        bars, feed = daily_bars(force)
        sma, lower, _ = rules.bollinger([b["c"] for b in bars], len(bars) - 1)
        pending = [{"ticker": force, "sector": WATCHLIST.get(force, "Unknown"), "signal_date": prev.isoformat(),
                    "close": bars[-1]["c"], "lower": lower, "sma": sma, "poc": rules.volume_profile_poc(bars),
                    "feed": feed, "forced": True}]

    if not pending:
        log({"action": "wait", "reason": "no pending signals"})
    for sig in pending:
        if sig["signal_date"] != (prev.isoformat() if prev else None):
            record_signal(sig, "skipped", f"stale: signal from {sig['signal_date']}, previous session was {prev}")
            continue
        enter_one(state, sig)


def enter_one(state: dict, sig: dict) -> None:
    today = date.today()
    ticker = sig["ticker"]
    why = rules.limit_violation(ticker, sig["sector"], state["spreads"], MAX_OPEN_TOTAL, MAX_PER_SECTOR,
                                MAX_PER_TICKER)
    if why:
        record_signal(sig, "skipped", why)
        return
    if IGNORE_EARNINGS:
        ok, earn, note = True, None, "ignored (--ignore-earnings test flag)"
        log({"action": "test_flag", "reason": "--ignore-earnings: earnings filter OFF for this dry run"})
    else:
        ok, earn, note = next_earnings(ticker)
    if not ok:
        record_signal(sig, "skipped", f"no earnings date ({note}) -- can't prove the expiry is earnings-free")
        return
    contracts = list_contracts(ticker,
                               expiration_date_gte=(today + timedelta(days=DTE_MIN)).isoformat(),
                               expiration_date_lte=(today + timedelta(days=DTE_MAX)).isoformat())
    exps = rules.eligible_expirations([as_date(c.expiration_date) for c in contracts], today, earn,
                                      DTE_MIN, DTE_MAX, MONTHLY_ONLY)
    if not exps:
        record_signal(sig, "skipped", f"no {'standard monthly ' if MONTHLY_ONLY else ''}expiration {DTE_MIN}-{DTE_MAX} DTE before earnings {earn}")
        return
    exp = exps[0]
    by_strike = {float(c.strike_price): c.symbol for c in contracts if as_date(c.expiration_date) == exp}
    width_goal = rules.target_width(sig["close"])
    poc_cap, band_cap = sig["poc"] * (1 - STRIKE_OFFSET), sig["lower"] * (1 - STRIKE_OFFSET)
    picked = rules.pick_strikes(sorted(by_strike), poc_cap, band_cap, width_goal)
    if picked is None:
        ceiling = min(poc_cap, band_cap)
        near = [k for k in sorted(by_strike) if ceiling - 40 <= k <= ceiling + 10]
        record_signal(sig, "skipped", f"no listed short/long pair below "
                                      f"min(POC {sig['poc']:.2f}, lower band {sig['lower']:.2f}) x {1 - STRIKE_OFFSET:.2f} for {exp}; "
                                      f"listed strikes near there: {near or 'none'} "
                                      f"({len(by_strike)} puts listed for {exp})", expiration=exp.isoformat())
        return
    short_k, long_k = picked
    width = round(short_k - long_k, 2)
    short_sym, long_sym = by_strike[short_k], by_strike[long_k]
    q = mids([short_sym, long_sym])
    if short_sym not in q or long_sym not in q:
        record_signal(sig, "skipped", "no two-sided quote on a leg", expiration=exp.isoformat(),
                      short_strike=short_k, long_strike=long_k)
        return
    credit = round(q[short_sym]["mid"] - q[long_sym]["mid"], 2)
    detail = dict(expiration=exp.isoformat(), short_strike=short_k, long_strike=long_k, mid_credit=credit,
                  width=width)
    if credit < MIN_CREDIT:
        record_signal(sig, "skipped", f"mid credit {credit:.2f} < {MIN_CREDIT:.2f}", **detail)
        return
    try:
        oid = submit_spread([(short_sym, PositionIntent.SELL_TO_OPEN), (long_sym, PositionIntent.BUY_TO_OPEN)],
                            QTY, credit, credit=True)
    except Exception as exc:                              # noqa: BLE001
        record_signal(sig, "skipped", f"order rejected: {exc}", **detail)
        return
    state["spreads"].append({
        "ticker": ticker, "sector": sig["sector"], "signal_date": sig["signal_date"],
        "entry_date": today.isoformat(), "expiration": exp.isoformat(),
        "short_symbol": short_sym, "long_symbol": long_sym, "short_strike": short_k, "long_strike": long_k,
        "width": width, "qty": QTY, "limit_credit": credit, "entry_credit": None, "entry_order_id": oid,
        "status": "opening", "tp_order_id": None, "close_order_id": None, "signal": sig})
    record_signal(sig, "order_placed", f"{'[TEST: earnings ignored] ' if IGNORE_EARNINGS else ''}earnings {earn}; {ticker} {exp} {short_k}/{long_k}P "
                                       f"(${width:g} wide, target {rules.WIDTH_PCT:.0%} of {sig['close']:.2f} = "
                                       f"${width_goal:.2f}); mid credit {credit:.2f} x{QTY}; "
                                       f"max loss ${(width - credit) * 100 * QTY:,.0f}", order_id=oid, **detail)


def run_manage(state: dict) -> None:
    today = date.today()
    for sp in state["spreads"]:
        if sp["status"] != "open":
            continue
        price = last_price(sp["ticker"])
        q = mids([sp["short_symbol"], sp["long_symbol"]])
        value = None
        if sp["short_symbol"] in q and sp["long_symbol"] in q:
            value = round(q[sp["short_symbol"]]["mid"] - q[sp["long_symbol"]]["mid"], 2)
        dte = (date.fromisoformat(sp["expiration"]) - today).days
        reason = rules.exit_reason(sp["entry_credit"], value, price, sp["short_strike"], dte)
        log({"action": "check", "ticker": sp["ticker"], "price": price, "spread_value": value,
             "entry_credit": sp["entry_credit"], "dte": dte, "exit": reason})
        if value is None:
            log({"action": "alert", "ticker": sp["ticker"], "reason": "no two-sided quote on a leg -- "
                 "only the price-based backup stop and time stop could be checked"})
        if not reason:
            continue
        if sp.get("tp_order_id"):
            status, filled, fap = cancel_and_confirm(sp["tp_order_id"]) if not DRY_RUN else ("canceled", 0, None)
            if status == "filled":
                record_trade(sp, fap, "take_profit (resting GTC)", sp["tp_order_id"])
                sp["status"] = "closed"
                continue
            sp["tp_order_id"] = None
        # marketable limit: pay the natural debit (short ask - long bid) so the exit fills
        if value is not None:
            limit = max(0.01, round(q[sp["short_symbol"]]["ask"] - q[sp["long_symbol"]]["bid"], 2))
        else:
            limit = round(sp["short_strike"] - sp["long_strike"], 2)   # no quotes: cap at the spread's max value
        try:
            oid = submit_spread([(sp["short_symbol"], PositionIntent.BUY_TO_CLOSE),
                                 (sp["long_symbol"], PositionIntent.SELL_TO_CLOSE)],
                                sp["qty"], limit, credit=False)
        except Exception as exc:                          # noqa: BLE001
            log({"action": "alert", "ticker": sp["ticker"], "reason": f"{reason} close rejected: {exc}"})
            continue
        sp.update(status="closing", exit_reason=reason, close_order_id=oid, close_limit=limit,
                  exit_mid=value)
        log({"action": "close_submitted", "ticker": sp["ticker"], "exit_reason": reason, "limit_debit": limit,
             "spread_mid": value, "order_id": oid})
    state["spreads"] = [s for s in state["spreads"] if s["status"] != "closed"]


def main() -> None:
    modes = [m for m in ("--signal", "--enter", "--manage") if m in sys.argv]
    if len(modes) != 1:
        print(__doc__)
        sys.exit(1)
    state = load_state()
    reconcile(state)
    {"--signal": run_signal, "--enter": run_enter, "--manage": run_manage}[modes[0]](state)
    save_state(state)
    book = [f"{s['ticker']} {s['expiration']} {s['short_strike']}/{s['long_strike']}P [{s['status']}]"
            for s in state["spreads"]]
    log({"action": "summary", "mode": modes[0], "spreads": book,
         "pending_signals": [p["ticker"] for p in state["pending_signals"]]})


if __name__ == "__main__":
    main()
