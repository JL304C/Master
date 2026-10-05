"""
DC Time Machine -- Alpaca PAPER feasibility probe.

Answers one question before anyone builds the full bot: can Alpaca carry the
double-calendar -> iron-condor ("time machine") trade end to end?
See dc_time_machine_strategy.md next to this script for the strategy itself.

Runs on SPY, not SPX. Alpaca rejects multi-leg orders whose European-style
legs have different expirations (HTTP 422, code 42210000, seen 2026-10-05),
which rules out both the calendar and the transformer on SPX/XSP as single
orders. SPY options are American-style, so that rule doesn't apply -- but:
  - early assignment is possible (low for ~35-delta OTM shorts, higher if
    price runs through a short strike, esp. right before an ex-dividend date);
  - SPY settles in shares, so the condor must be CLOSED on expiration day,
    never left to expire (`status` warns when that day arrives);
  - no Section 1256 60/40 tax treatment.

Subcommands (run them in this order):

  check      Read-only. Account options level, whether SPY contracts and
             chain snapshots (quotes, IV, greeks) come back, the expiration pair and ~35-delta strikes the
             strategy would pick, the calendar's debit and the transformer's
             current credit vs. the risk-free minimum. Places no orders.
  open       Places ONE 1-lot double calendar as a single 4-leg order,
             starting at mid and conceding $0.01/minute up to --max-slip.
  transform  Submits the transformer (sell back-month longs, buy $1-wide
             front-month wings) as ONE 4-leg order at C_min = D + W + fees.
             THE KEY TEST: does Alpaca accept this order at all?
  status     Shows the probe's saved state, its open orders and positions.
  close      Closes whatever the probe still holds (calendar or condor).

State lives in dc_probe_state.json; every event is appended to
dc_probe_log.jsonl. Both sit next to this script.

Paper trading only: paper=True is hard-coded below.

Requires: pip install alpaca-py
Credentials: ALPACA_API_KEY / ALPACA_SECRET_KEY as environment variables, or a
.env file next to this script.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from alpaca.common.exceptions import APIError
from alpaca.data.historical import OptionHistoricalDataClient, StockHistoricalDataClient
from alpaca.data.requests import OptionChainRequest, StockLatestQuoteRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import (
    AssetStatus,
    OrderClass,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionIntent,
    TimeInForce,
)
from alpaca.trading.requests import (
    GetOptionContractsRequest,
    GetOrderByIdRequest,
    LimitOrderRequest,
    OptionLegRequest,
)

# --------------------------------------------------------------------------- #
# Strategy parameters (spec sections 3 and 4).
# --------------------------------------------------------------------------- #
UNDERLYING = "SPY"
ROOT = "SPY"
FRONT_DTE_MIN = 6            # front (short) expiration window
FRONT_DTE_MAX = 15
BACK_GAP_MIN = 1             # back expiration is 1-4 calendar days after front
BACK_GAP_MAX = 4
TARGET_DELTA = 0.35          # 30-40 delta short strikes
WING = 1.0                   # W, wing width in dollars (SPY strikes are $1 apart)
QTY = 1                      # probe size: one double calendar

# Costs. Alpaca's options commission is $0; what's left are small regulatory
# and clearing fees (ORF, OCC, TAF on sells). 0.05/contract/leg is a
# conservative placeholder -- replace it with what your fills actually show.
FEE_PER_LEG_CONTRACT = 0.05
ROUND_TRIP_LEGS = 12         # 4 to open the calendar + 4 to transform + 4 to
                             # close the condor on expiration day (SPY settles
                             # in shares, so it can't just expire like SPX)

TICK = 0.01                  # SPY options trade in pennies
RATE = 0.04                  # risk-free rate, for put-call parity and
                             # fallback deltas

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "dc_probe_state.json"
LOG_JSONL = HERE / "dc_probe_log.jsonl"


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

# paper=True is hard-coded here on purpose -- this is a feasibility probe.
trade_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
option_data = OptionHistoricalDataClient(API_KEY, SECRET_KEY)
stock_data = StockHistoricalDataClient(API_KEY, SECRET_KEY)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def log(event: dict) -> None:
    event = {"ts": datetime.now(timezone.utc).isoformat(), **event}
    with LOG_JSONL.open("a") as fh:
        fh.write(json.dumps(event, default=str) + "\n")
    print(json.dumps(event, indent=2, default=str))


def say(msg: str) -> None:
    print(msg, flush=True)


def load_state() -> dict:
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


def round_tick(x: float) -> float:
    return round(round(x / TICK) * TICK, 2)


def ceil_tick(x: float) -> float:
    return round(math.ceil(x / TICK - 1e-9) * TICK, 2)


def parse_occ(sym: str) -> dict:
    """SPY261016P00670000 -> root/expiration/right/strike."""
    i = 0
    while i < len(sym) and not sym[i].isdigit():
        i += 1
    rest = sym[i:]
    return {
        "root": sym[:i],
        "expiration": datetime.strptime(rest[:6], "%y%m%d").date(),
        "right": rest[6],
        "strike": int(rest[7:]) / 1000.0,
    }


def occ(exp: date, right: str, strike: float) -> str:
    return f"{ROOT}{exp:%y%m%d}{right}{int(round(strike * 1000)):08d}"


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def black76_delta(fwd: float, strike: float, t: float, iv: float, right: str) -> float:
    d1 = (math.log(fwd / strike) + 0.5 * iv * iv * t) / (iv * math.sqrt(t))
    disc = math.exp(-RATE * t)
    return disc * norm_cdf(d1) if right == "C" else -disc * norm_cdf(-d1)


def api_error_text(e: Exception) -> str:
    """Alpaca's rejection message is the whole point of this probe -- keep it verbatim."""
    if isinstance(e, APIError):
        return f"HTTP {getattr(e, 'status_code', '?')}: {e}"
    return f"{type(e).__name__}: {e}"


# --------------------------------------------------------------------------- #
# Market data
# --------------------------------------------------------------------------- #
class Quote:
    def __init__(self, snap):
        q = snap.latest_quote
        self.bid = float(q.bid_price) if q and q.bid_price else 0.0
        self.ask = float(q.ask_price) if q and q.ask_price else 0.0
        self.iv = float(snap.implied_volatility) if snap.implied_volatility else None
        self.delta = float(snap.greeks.delta) if snap.greeks else None
        self.quote_time = q.timestamp if q else None

    @property
    def ok(self) -> bool:
        return self.ask > 0 and self.ask >= self.bid

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0


def spot_price() -> float:
    q = stock_data.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=UNDERLYING))[UNDERLYING]
    return (float(q.bid_price) + float(q.ask_price)) / 2.0


def fetch_chain(underlying: str, exp_from: date, exp_to: date, lo: float, hi: float) -> dict[str, Quote]:
    req = OptionChainRequest(
        underlying_symbol=underlying,
        root_symbol=ROOT,
        expiration_date_gte=exp_from,
        expiration_date_lte=exp_to,
        strike_price_gte=lo,
        strike_price_lte=hi,
    )
    snaps = option_data.get_option_chain(req)
    return {sym: Quote(s) for sym, s in snaps.items()}


def get_chain(exp_from: date, exp_to: date, lo: float, hi: float) -> tuple[str, dict[str, Quote]]:
    try:
        chain = fetch_chain(UNDERLYING, exp_from, exp_to, lo, hi)
    except Exception as e:
        raise RuntimeError(f"no {UNDERLYING} option chain data: {api_error_text(e)}")
    if not chain:
        raise RuntimeError(f"no {UNDERLYING} option chain data: empty chain")
    return UNDERLYING, chain


def forward_from_parity(chain: dict[str, Quote], exp: date) -> float | None:
    """F = K + (C - P) * e^{rT}, averaged over the 3 strikes where C ~= P."""
    t = max((exp - date.today()).days, 1) / 365.0
    pairs = []
    for sym, q in chain.items():
        p = parse_occ(sym)
        if p["expiration"] != exp or p["right"] != "C" or not q.ok:
            continue
        put = chain.get(occ(exp, "P", p["strike"]))
        if put and put.ok:
            pairs.append((abs(q.mid - put.mid), p["strike"] + (q.mid - put.mid) * math.exp(RATE * t)))
    if not pairs:
        return None
    pairs.sort()
    best = [f for _, f in pairs[:3]]
    return sum(best) / len(best)


# --------------------------------------------------------------------------- #
# Strategy selection (spec section 3)
# --------------------------------------------------------------------------- #
def pick_expirations(expirations: list[date]) -> tuple[date, date] | None:
    """Front: 6-15 DTE, preferring a Friday. Back: 1-4 days later, preferring
    the following Monday (Steve's favourite pairing)."""
    today = date.today()
    exps = sorted(set(expirations))
    fronts = [e for e in exps if FRONT_DTE_MIN <= (e - today).days <= FRONT_DTE_MAX]
    fronts.sort(key=lambda e: (e.weekday() != 4, e))   # Fridays first, nearest first
    for f in fronts:
        backs = [e for e in exps if BACK_GAP_MIN <= (e - f).days <= BACK_GAP_MAX]
        backs.sort(key=lambda e: (e.weekday() != 0, e))  # Mondays first
        if backs:
            return f, backs[0]
    return None


def option_delta(sym: str, q: Quote, fwd: float) -> tuple[float | None, str]:
    p = parse_occ(sym)
    if q.delta is not None:
        return q.delta, "alpaca"
    t = max((p["expiration"] - date.today()).days, 1) / 365.0
    if q.iv:
        return black76_delta(fwd, p["strike"], t, q.iv, p["right"]), "computed_from_alpaca_iv"
    iv = implied_vol_from_mid(q.mid, fwd, p["strike"], t, p["right"])
    if iv:
        q.iv = iv   # so the IV ratio can use it too
        return black76_delta(fwd, p["strike"], t, iv, p["right"]), "computed_from_mid_price"
    return None, "missing"


def black76_price(fwd: float, strike: float, t: float, iv: float, right: str) -> float:
    sd = iv * math.sqrt(t)
    d1 = (math.log(fwd / strike) + 0.5 * sd * sd) / sd
    d2 = d1 - sd
    disc = math.exp(-RATE * t)
    if right == "C":
        return disc * (fwd * norm_cdf(d1) - strike * norm_cdf(d2))
    return disc * (strike * norm_cdf(-d2) - fwd * norm_cdf(-d1))


def implied_vol_from_mid(price: float, fwd: float, strike: float, t: float, right: str) -> float | None:
    """Bisection on Black-76. Used when Alpaca's feed has no IV/greeks. Treats
    the American SPY options as European, which is close enough for short-dated
    OTM strikes."""
    if price <= 0:
        return None
    lo, hi = 0.01, 3.0
    if not (black76_price(fwd, strike, t, lo, right) < price < black76_price(fwd, strike, t, hi, right)):
        return None
    for _ in range(60):
        mid = (lo + hi) / 2
        if black76_price(fwd, strike, t, mid, right) < price:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


PICK_DIAG: dict = {}   # why candidate strikes were skipped, reported on failure


def pick_strike(chain: dict[str, Quote], front: date, back: date, right: str, fwd: float) -> dict | None:
    """Strike nearest TARGET_DELTA on the OTM side whose back-month twin and
    5-wide front-month wing are both quoted."""
    best = None
    why = PICK_DIAG.setdefault(right, {"front_otm_strikes": 0, "front_no_quote": 0, "back_missing": 0,
                                       "back_no_quote": 0, "wing_missing": 0, "wing_no_quote": 0,
                                       "no_delta": 0, "usable": 0})
    for sym, q in chain.items():
        p = parse_occ(sym)
        if p["expiration"] != front or p["right"] != right:
            continue
        k = p["strike"]
        if (right == "P" and k >= fwd) or (right == "C" and k <= fwd):
            continue
        why["front_otm_strikes"] += 1
        if not q.ok:
            why["front_no_quote"] += 1
            continue
        wing_k = k - WING if right == "P" else k + WING
        back_q = chain.get(occ(back, right, k))
        wing_q = chain.get(occ(front, right, wing_k))
        if back_q is None:
            why["back_missing"] += 1
            continue
        if not back_q.ok:
            why["back_no_quote"] += 1
            continue
        if wing_q is None:
            why["wing_missing"] += 1
            continue
        if not wing_q.ok:
            why["wing_no_quote"] += 1
            continue
        d, src = option_delta(sym, q, fwd)
        if d is None:
            why["no_delta"] += 1
            continue
        why["usable"] += 1
        err = abs(abs(d) - TARGET_DELTA)
        if best is None or err < best["err"]:
            best = {"err": err, "strike": k, "delta": d, "delta_source": src,
                    "front": sym, "back": occ(back, right, k), "wing": occ(front, right, wing_k)}
    return best


def build_plan() -> dict:
    """Everything `check` reports and `open` needs, from one chain pull."""
    today = date.today()
    spot0 = spot_price()
    underlying, chain = get_chain(
        today + timedelta(days=FRONT_DTE_MIN),
        today + timedelta(days=FRONT_DTE_MAX + BACK_GAP_MAX),
        spot0 * 0.92, spot0 * 1.08,
    )
    exps = [parse_occ(s)["expiration"] for s in chain]
    pair = pick_expirations(exps)
    if pair is None:
        raise RuntimeError(f"no front/back expiration pair in chain; expirations seen: {sorted(set(exps))}")
    front, back = pair
    fwd = forward_from_parity(chain, front)
    if fwd is None:
        raise RuntimeError("could not compute the forward from put-call parity (no quoted call/put pairs)")
    put = pick_strike(chain, front, back, "P", fwd)
    call = pick_strike(chain, front, back, "C", fwd)
    if not put or not call:
        raise RuntimeError(
            "could not find ~35-delta strikes with back-month and wing contracts quoted.\n"
            f"  front={front} back={back} forward(parity)={fwd:.2f} chain contracts={len(chain)}\n"
            f"  put candidates:  {PICK_DIAG.get('P')}\n"
            f"  call candidates: {PICK_DIAG.get('C')}\n"
            "  ('no_quote' = bid/ask missing or zero, usual before 9:30 am ET or on the free feed)")

    q = {s: chain[s] for s in (put["front"], put["back"], put["wing"], call["front"], call["back"], call["wing"])}
    debit_mid = (q[put["back"]].mid - q[put["front"]].mid) + (q[call["back"]].mid - q[call["front"]].mid)
    debit_natural = (q[put["back"]].ask - q[put["front"]].bid) + (q[call["back"]].ask - q[call["front"]].bid)
    xform_mid = q[put["back"]].mid + q[call["back"]].mid - q[put["wing"]].mid - q[call["wing"]].mid
    fees_pts = FEE_PER_LEG_CONTRACT * ROUND_TRIP_LEGS / 100.0

    def avg_iv(syms):
        for s in syms:
            if not q[s].iv:
                option_delta(s, q[s], fwd)   # fills q[s].iv from the mid price
        ivs = [q[s].iv for s in syms if q[s].iv]
        return sum(ivs) / len(ivs) if ivs else None

    front_iv = avg_iv([put["front"], call["front"]])
    back_iv = avg_iv([put["back"], call["back"]])
    quotes_with_iv = sum(1 for v in chain.values() if v.iv)
    quotes_with_delta = sum(1 for v in chain.values() if v.delta is not None)

    return {
        "chain_underlying_that_worked": underlying,
        "chain_contracts": len(chain),
        "chain_contracts_with_iv": quotes_with_iv,
        "chain_contracts_with_delta": quotes_with_delta,
        "spy_quote_mid": round(spot0, 2),
        "forward_from_parity": round(fwd, 2),
        "front_expiration": front,
        "back_expiration": back,
        "put": put,
        "call": call,
        "quotes": {s: {"bid": v.bid, "ask": v.ask, "iv": v.iv, "delta": v.delta, "time": v.quote_time} for s, v in q.items()},
        "calendar_debit_mid": round(debit_mid, 2),
        "calendar_debit_natural": round(debit_natural, 2),
        "front_iv": front_iv,
        "back_iv": back_iv,
        "iv_ratio_front_over_back": round(front_iv / back_iv, 4) if front_iv and back_iv else None,
        "fees_points": round(fees_pts, 4),
        "c_min_if_filled_at_mid": ceil_tick(debit_mid + WING + fees_pts),
        "transformer_credit_mid_now": round(xform_mid, 2),
    }


# --------------------------------------------------------------------------- #
# Orders
# --------------------------------------------------------------------------- #
def leg(symbol: str, side: OrderSide, intent: PositionIntent) -> OptionLegRequest:
    return OptionLegRequest(symbol=symbol, ratio_qty=1, side=side, position_intent=intent)


def submit_mleg(legs: list[OptionLegRequest], qty: int, debit: float):
    """Alpaca's mleg sign convention: positive limit = net debit, negative = net credit."""
    req = LimitOrderRequest(
        qty=qty,
        order_class=OrderClass.MLEG,
        type=OrderType.LIMIT,
        time_in_force=TimeInForce.DAY,     # the only TIF Alpaca takes for options
        limit_price=round_tick(debit),
        legs=legs,
    )
    return trade_client.submit_order(req)


def get_nested(order_id) -> object:
    return trade_client.get_order_by_id(order_id, GetOrderByIdRequest(nested=True))


def net_fill_debit(order) -> float | None:
    """Net debit per spread from the legs' own fills (sign-safe, unlike the
    parent's filled_avg_price whose sign convention isn't documented)."""
    if not order.legs:
        return None
    total = 0.0
    for lg in order.legs:
        if lg.filled_avg_price is None:
            return None
        px = float(lg.filled_avg_price) * float(lg.ratio_qty or 1)
        total += px if lg.side == OrderSide.BUY else -px
    return round(total, 4)


def work_order(legs, qty: int, start_debit: float, max_slip: float, label: str):
    """Submit at start_debit, then every minute cancel/resubmit one tick ($0.01) worse,
    up to max_slip. Returns the filled order, or None (nothing left working)."""
    debit = round_tick(start_debit)
    limit = round_tick(start_debit + max_slip)
    while True:
        try:
            order = submit_mleg(legs, qty, debit)
        except Exception as e:
            log({"action": f"{label}_rejected", "limit_debit": debit, "error": api_error_text(e)})
            return None
        log({"action": f"{label}_submitted", "order_id": order.id, "limit_debit": debit, "status": order.status})
        deadline = time.time() + 60
        while time.time() < deadline:
            time.sleep(5)
            o = get_nested(order.id)
            if o.status == OrderStatus.FILLED:
                log({"action": f"{label}_filled", "order_id": o.id, "limit_debit": debit,
                     "net_fill_debit": net_fill_debit(o), "parent_filled_avg_price": o.filled_avg_price})
                return o
            if o.status in (OrderStatus.REJECTED, OrderStatus.CANCELED, OrderStatus.EXPIRED):
                log({"action": f"{label}_{o.status.value}", "order_id": o.id, "limit_debit": debit})
                return None
        try:
            trade_client.cancel_order_by_id(order.id)
        except Exception as e:
            say(f"cancel failed ({api_error_text(e)}); re-checking order")
        time.sleep(2)
        o = get_nested(order.id)
        if o.status == OrderStatus.FILLED:   # filled while we were cancelling
            log({"action": f"{label}_filled", "order_id": o.id, "limit_debit": debit,
                 "net_fill_debit": net_fill_debit(o), "parent_filled_avg_price": o.filled_avg_price})
            return o
        if o.status not in (OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.REJECTED):
            log({"action": f"{label}_stuck", "order_id": o.id, "status": o.status,
                 "reason": "could not confirm cancel -- check the dashboard before doing anything else"})
            return None
        if debit + TICK > limit + 1e-9:
            log({"action": f"{label}_gave_up", "last_limit_debit": debit, "max_debit": limit})
            return None
        debit = round_tick(debit + TICK)


def require_market_open() -> None:
    clock = trade_client.get_clock()
    if not clock.is_open:
        say(f"Market is closed (next open {clock.next_open}). Options orders need regular hours.")
        sys.exit(1)


# --------------------------------------------------------------------------- #
# Subcommands
# --------------------------------------------------------------------------- #
def cmd_check(args) -> None:
    clock = trade_client.get_clock()
    say(f"== Market: {'OPEN' if clock.is_open else 'CLOSED (quotes may be empty or stale; next open ' + str(clock.next_open) + ')'} ==")
    acct = trade_client.get_account()
    say("== Account ==")
    say(f"  options_approved_level={acct.options_approved_level} "
        f"options_trading_level={acct.options_trading_level} "
        f"options_buying_power={acct.options_buying_power}")
    if (acct.options_trading_level or 0) < 3:
        say("  !! Multi-leg spreads need options level 3 -- raise it in the paper dashboard.")

    say("== Contract listing (trading API) ==")
    for und in (UNDERLYING,):
        try:
            resp = trade_client.get_option_contracts(GetOptionContractsRequest(
                underlying_symbols=[und], root_symbol=ROOT, status=AssetStatus.ACTIVE,
                expiration_date_gte=date.today(),
                expiration_date_lte=date.today() + timedelta(days=FRONT_DTE_MAX + BACK_GAP_MAX),
                limit=50,
            ))
            cs = resp.option_contracts or []
            say(f"  underlying={und}: {len(cs)} contracts on first page"
                + (f", e.g. {cs[0].symbol} style={cs[0].style} tradable={cs[0].tradable}" if cs else ""))
        except Exception as e:
            say(f"  underlying={und}: {api_error_text(e)}")

    say("== Chain + strategy selection (market data API) ==")
    plan = build_plan()
    log({"action": "check", **plan})
    say("")
    say(f"Double calendar the strategy would open now: put {plan['put']['strike']:.0f} / call {plan['call']['strike']:.0f}, "
        f"sell {plan['front_expiration']} / buy {plan['back_expiration']}")
    say(f"  debit mid {plan['calendar_debit_mid']:.2f}, natural {plan['calendar_debit_natural']:.2f}")
    say(f"  risk-free credit needed if filled at mid: {plan['c_min_if_filled_at_mid']:.2f} "
        f"(D + {WING:.2f} + fees {plan['fees_points']:.2f}); transformer worth {plan['transformer_credit_mid_now']:.2f} right now")
    say(f"  IV ratio front/back: {plan['iv_ratio_front_over_back']}")


def cmd_open(args) -> None:
    state = load_state()
    if state.get("phase") in ("calendar", "condor"):
        say(f"Probe already holds a {state['phase']} (see dc_probe_state.json). Run `close` first.")
        sys.exit(1)
    require_market_open()
    plan = build_plan()
    p, c = plan["put"], plan["call"]
    legs = [
        leg(p["front"], OrderSide.SELL, PositionIntent.SELL_TO_OPEN),
        leg(p["back"], OrderSide.BUY, PositionIntent.BUY_TO_OPEN),
        leg(c["front"], OrderSide.SELL, PositionIntent.SELL_TO_OPEN),
        leg(c["back"], OrderSide.BUY, PositionIntent.BUY_TO_OPEN),
    ]
    log({"action": "open_plan", **plan})
    order = work_order(legs, QTY, plan["calendar_debit_mid"], args.max_slip, "open_calendar")
    if order is None:
        say("Double calendar did not fill. Nothing is open.")
        return
    d = net_fill_debit(order)
    if d is None:
        d = abs(float(order.filled_avg_price))
        say(f"!! Leg fills missing; using parent filled_avg_price {d} as D -- verify on the dashboard.")
    fees = FEE_PER_LEG_CONTRACT * ROUND_TRIP_LEGS / 100.0
    state = {
        "phase": "calendar",
        "qty": QTY,
        "debit": d,
        "c_min": ceil_tick(d + WING + fees),
        "put": p, "call": c,
        "front_expiration": plan["front_expiration"],
        "back_expiration": plan["back_expiration"],
        "open_order_id": str(order.id),
        "chain_underlying": plan["chain_underlying_that_worked"],
    }
    save_state(state)
    say(f"Calendar filled at D={d:.2f}. Risk-free transformer credit C_min={state['c_min']:.2f}. "
        f"Next: python dc_probe.py transform")


def cmd_transform(args) -> None:
    state = load_state()
    if state.get("phase") != "calendar":
        say("No open probe calendar in dc_probe_state.json. Run `open` first.")
        sys.exit(1)
    require_market_open()
    p, c = state["put"], state["call"]
    credit = args.credit if args.credit is not None else state["c_min"]
    if credit < state["c_min"] and not args.allow_below_min:
        say(f"Credit {credit:.2f} is below C_min {state['c_min']:.2f}: the condor could lose money. "
            f"Pass --allow-below-min to test acceptance anyway.")
        sys.exit(1)
    n = credit - state["debit"]
    log({"action": "transform_plan", "credit": credit, "debit": state["debit"],
         "max_profit_per_contract": round(n * 100, 2),
         "worst_case_per_contract_before_fees": round((n - WING) * 100, 2)})
    legs = [
        leg(p["back"], OrderSide.SELL, PositionIntent.SELL_TO_CLOSE),
        leg(c["back"], OrderSide.SELL, PositionIntent.SELL_TO_CLOSE),
        leg(p["wing"], OrderSide.BUY, PositionIntent.BUY_TO_OPEN),
        leg(c["wing"], OrderSide.BUY, PositionIntent.BUY_TO_OPEN),
    ]
    try:
        order = submit_mleg(legs, state["qty"], -credit)
    except Exception as e:
        log({"action": "transform_REJECTED", "credit": credit, "error": api_error_text(e)})
        say("\nRESULT: Alpaca REJECTED the transformer order. The error above is the answer to the "
            "feasibility question -- send it back for review.")
        return
    state["transform_order_id"] = str(order.id)
    save_state(state)
    log({"action": "transform_ACCEPTED", "order_id": order.id, "status": order.status, "credit": credit})
    say("\nRESULT: Alpaca ACCEPTED the transformer order. It is a DAY order working at "
        f"{credit:.2f} credit; it fills only if the calendar gains enough today.")
    if args.wait <= 0:
        say("Run `status` later to see whether it filled.")
        return
    deadline = time.time() + args.wait * 60
    while time.time() < deadline:
        time.sleep(15)
        o = get_nested(order.id)
        if o.status == OrderStatus.FILLED:
            state["phase"] = "condor"
            state["transform_credit"] = -net_fill_debit(o) if net_fill_debit(o) is not None else credit
            save_state(state)
            log({"action": "transform_filled", "order_id": o.id, "credit": state["transform_credit"]})
            return
        if o.status in (OrderStatus.REJECTED, OrderStatus.CANCELED, OrderStatus.EXPIRED):
            log({"action": f"transform_{o.status.value}", "order_id": o.id})
            return
    say("Still working after --wait minutes; it stays live until the close. Run `status` later.")


def probe_symbols(state: dict) -> set[str]:
    return {state[side][k] for side in ("put", "call") for k in ("front", "back", "wing")} if state else set()


def cmd_status(args) -> None:
    state = load_state()
    say(json.dumps(state, indent=2, default=str) if state else "No probe state.")
    tid = state.get("transform_order_id")
    if tid and state.get("phase") == "calendar":
        o = get_nested(tid)
        say(f"Transformer order {tid}: {o.status}")
        if o.status == OrderStatus.FILLED:
            nd = net_fill_debit(o)
            state["phase"] = "condor"
            state["transform_credit"] = -nd if nd is not None else state["c_min"]
            save_state(state)
            say(f"Filled -> now an iron condor, credit {state['transform_credit']:.2f}, "
                f"locked-in N = {state['transform_credit'] - state['debit']:.2f}")
    syms = probe_symbols(state)
    say("Positions:")
    positions = trade_client.get_all_positions()
    for pos in positions:
        if not syms or pos.symbol in syms:
            say(f"  {pos.symbol} qty={pos.qty} avg={pos.avg_entry_price} mkt={pos.market_value} upl={pos.unrealized_pl}")
    if any(p.symbol == UNDERLYING for p in positions):
        say(f"!! You hold {UNDERLYING} shares -- likely an early assignment. Check the dashboard.")
    if state.get("phase") in ("calendar", "condor") and str(state.get("front_expiration")) == date.today().isoformat():
        say("!! Front expiration is TODAY. SPY settles in shares: run `close` before 3:45 pm ET.")


def cmd_close(args) -> None:
    state = load_state()
    syms = probe_symbols(state)
    if not syms:
        say("No probe state -- nothing to close.")
        return
    require_market_open()
    tid = state.get("transform_order_id")
    if tid:
        o = get_nested(tid)
        if o.status not in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.REJECTED):
            trade_client.cancel_order_by_id(tid)
            say(f"Cancelled working transformer {tid}.")
            time.sleep(2)
    held = [p for p in trade_client.get_all_positions() if p.symbol in syms]
    if not held:
        say("Probe holds no positions (expired or already closed). Clearing state.")
        STATE_FILE.unlink(missing_ok=True)
        return
    qtys = {abs(int(float(p.qty))) for p in held}
    if len(qtys) != 1 or len(held) > 4:
        say(f"Unexpected position shape {[(p.symbol, p.qty) for p in held]} -- close manually on the dashboard.")
        return
    qty = qtys.pop()
    snaps = option_data.get_option_chain(OptionChainRequest(
        underlying_symbol=state.get("chain_underlying", UNDERLYING), root_symbol=ROOT,
        expiration_date_gte=min(parse_occ(p.symbol)["expiration"] for p in held),
        expiration_date_lte=max(parse_occ(p.symbol)["expiration"] for p in held),
        strike_price_gte=min(parse_occ(p.symbol)["strike"] for p in held),
        strike_price_lte=max(parse_occ(p.symbol)["strike"] for p in held),
    ))
    legs, debit = [], 0.0
    for p in held:
        q = Quote(snaps[p.symbol]) if p.symbol in snaps else None
        if not (q and q.ok):
            say(f"No usable quote for {p.symbol}; close manually on the dashboard.")
            return
        mid = q.mid
        if float(p.qty) > 0:
            legs.append(leg(p.symbol, OrderSide.SELL, PositionIntent.SELL_TO_CLOSE))
            debit -= mid
        else:
            legs.append(leg(p.symbol, OrderSide.BUY, PositionIntent.BUY_TO_CLOSE))
            debit += mid
    if len(legs) == 1:
        say(f"Only one leg left ({held[0].symbol}); close it on the dashboard -- not worth a combo order.")
        return
    order = work_order(legs, qty, debit, args.max_slip, "close")
    if order is not None:
        state["phase"] = "closed"
        state["close_net_debit"] = net_fill_debit(order)
        save_state(state)
        say("Closed. State kept in dc_probe_state.json for the record; delete it before the next `open`.")


def main() -> None:
    ap = argparse.ArgumentParser(description="DC Time Machine feasibility probe (Alpaca PAPER only).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="read-only data and selection check")
    o = sub.add_parser("open", help="open one 1-lot double calendar")
    o.add_argument("--max-slip", type=float, default=0.05,
                   help="max dollars to concede above mid while working the fill (default 0.05)")
    t = sub.add_parser("transform", help="submit the transformer order at C_min")
    t.add_argument("--credit", type=float, help="override the credit (default: C_min from state)")
    t.add_argument("--allow-below-min", action="store_true", help="permit a credit below C_min")
    t.add_argument("--wait", type=float, default=0, help="minutes to watch for a fill (default: don't wait)")
    sub.add_parser("status", help="show state, transformer order and positions")
    c = sub.add_parser("close", help="close whatever the probe still holds")
    c.add_argument("--max-slip", type=float, default=0.15,
                   help="max dollars to concede past mid while closing (default 0.15)")
    args = ap.parse_args()
    {"check": cmd_check, "open": cmd_open, "transform": cmd_transform,
     "status": cmd_status, "close": cmd_close}[args.cmd](args)


if __name__ == "__main__":
    main()
