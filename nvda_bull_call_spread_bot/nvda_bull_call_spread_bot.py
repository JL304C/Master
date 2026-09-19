"""
NVDA bull call debit spread -- scheduled paper-trading bot for Alpaca.

Implements exactly the strategy settled in tsla_bull_call_spread/README.md
after the audit/backtest iteration there:
  - Entry only when: (1) 3 consecutive daily closes above the 8-day EMA,
    (2) not inside the calendar week of a known/expected earnings date,
    (3) no position and no pending order already open.
  - Construction: buy the call closest to 30 delta, sell the call whose
    strike is closest to (long strike + $10), same expiration, ~30 DTE.
    Entered as ONE multi-leg limit order at a net debit near the midpoint.
  - Entry gate: net debit <= 30% of the $10 width and <= $3.00/share, and
    implied reward:risk >= 2.3x -- otherwise skip this cycle, don't chase.
  - Exit: close both legs together (via two close_position calls, Alpaca's
    own documented pattern for closing a multi-leg position -- see
    alpacahq/alpaca-py examples/options/options-bull-call-spread.ipynb) the
    first time the spread's value reaches 45% of the debit paid, OR when
    <=2 days remain to expiration, whichever comes first. NO stop-loss --
    this is a defined-risk spread; the max loss is already capped at the
    debit, so a stop only converts recoverable trades into early partial
    losses (see tsla_bull_call_spread/stop_pct_sweep_output_2026-09-19.txt).
  - Never add to, roll, or adjust either leg. One spread at a time.

Delta/IV are NOT pulled from Alpaca's option-chain snapshot endpoint.
Following Alpaca's own reference notebook for this exact strategy
(alpacahq/alpaca-py examples/options/options-bull-call-spread.ipynb), this
script backs out each candidate contract's own implied vol from its live
quote (bisection on Black-Scholes, since no chain-wide IV/greeks feed is
assumed available) and computes delta from that -- the same math already
validated in tsla_bull_call_spread/nvda_final_strategy_audit.py, now fed
with real market quotes instead of a single assumed sigma.

Derives all state fresh from Alpaca's own account each run (open positions'
own avg_entry_price, each option symbol's own encoded strike/expiration) --
no separate local state file to drift out of sync with reality.

Meant to run once per trading day via Windows Task Scheduler (after the
prior session's daily bar is final -- e.g. a few minutes after next open).
Paper trading only: this script never sets paper=False or points at the
live endpoint. Going live later means writing that decision explicitly,
not flipping a flag here.

STATUS (disclosed): built from Alpaca's own verified reference notebook for
the multi-leg order and close_position calls. Every import, request class,
enum member, and model field this script touches (GetOptionContractsRequest,
OptionLatestQuoteRequest, LimitOrderRequest+OrderClass.MLEG+OptionLegRequest,
ClosePositionRequest, GetOrdersRequest, StockBarsRequest, Position.side,
Position.avg_entry_price, Order.legs, PositionSide.SHORT, ...) was checked
by installing alpaca-py 0.44.0 and constructing each object directly against
the real SDK -- not just written from memory. What that check CANNOT cover,
with no live account reachable from the environment that wrote this: actual
order fills, real quote data shapes at runtime, and end-to-end timing.
Treat the first manual paper run as the real integration test (see
README.md) and read its output closely rather than trusting it blind.

Requires: pip install alpaca-py
Credentials: set ALPACA_API_KEY / ALPACA_SECRET_KEY as environment
variables, or create a .env file next to this script (see load_env_file).
"""

from __future__ import annotations

import csv
import json
import os
import sys
import math
from datetime import datetime, timedelta, date, timezone
from pathlib import Path

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    LimitOrderRequest,
    GetOptionContractsRequest,
    GetOrdersRequest,
    ClosePositionRequest,
    OptionLegRequest,
)
from alpaca.trading.enums import (
    AssetStatus,
    OrderSide,
    OrderClass,
    TimeInForce,
    ContractType,
    QueryOrderStatus,
    PositionSide,
)
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import (
    StockLatestTradeRequest,
    StockBarsRequest,
    OptionLatestQuoteRequest,
)

# --------------------------------------------------------------------------- #
# Strategy parameters -- exactly what was audited/backtested. Change these
# and you are running a DIFFERENT, un-audited strategy.
# --------------------------------------------------------------------------- #
SYMBOL = "NVDA"
WIDTH = 10.0                 # short strike = long strike + WIDTH
TARGET_DELTA = 0.30          # long call target delta
EMA_LEN = 8                  # entry signal EMA length
SIGNAL_DAYS = 3              # consecutive closes above EMA required to enter
DTE_TARGET = 30
DTE_WINDOW = 5                # search expirations DTE_TARGET +/- this many days
PROFIT_TARGET = 0.45          # close all at this fraction of debit as profit
CLOSE_BY_DTE = 2              # close (target or not) once this few days remain
MAX_DEBIT_RATIO = 0.30        # debit must be <= this fraction of WIDTH
MAX_DEBIT_DOLLARS = 3.00      # ...and never above this, whichever binds first
MIN_REWARD_RISK = 2.3         # (WIDTH - debit) / debit must clear this
RISK_FREE_RATE = 0.04
OI_THRESHOLD = 50             # skip contracts with open interest at/below this
STRIKE_SEARCH_RANGE = 0.30    # search calls within +/-30% of spot for the 30-delta leg

# Known/expected NVDA earnings dates -- UPDATE THIS EACH QUARTER. See
# tsla_bull_call_spread/nvda_ema_signal_backtest.py for how these were
# sourced (Alpha Vantage's EARNINGS endpoint for real past dates; future
# dates are estimates until confirmed).
EARNINGS_DATES = [date(2026, 8, 26), date(2026, 11, 18)]

HERE = Path(__file__).resolve().parent
LOG_JSONL = HERE / "nvda_bull_call_spread_log.jsonl"
LOG_CSV = HERE / "nvda_bull_call_spread_trades.csv"


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
stock_data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)
option_data_client = OptionHistoricalDataClient(API_KEY, SECRET_KEY)


def log(event: dict) -> None:
    event = {"ts": datetime.now(timezone.utc).isoformat(), **event}
    with LOG_JSONL.open("a") as fh:
        fh.write(json.dumps(event) + "\n")
    is_new = not LOG_CSV.exists()
    with LOG_CSV.open("a", newline="") as fh:
        w = csv.writer(fh)
        if is_new:
            w.writerow(["timestamp", "action", "symbol", "qty", "reason"])
        w.writerow([
            event["ts"], event.get("action", ""), event.get("symbol", ""),
            event.get("qty", ""), event.get("reason", ""),
        ])
    print(json.dumps(event, indent=2))


# --------------------------------------------------------------------------- #
# Black-Scholes (same math as tsla_bull_call_spread/nvda_final_strategy_audit.py)
# --------------------------------------------------------------------------- #
def norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_call(S, K, T, r, sigma) -> float:
    if T <= 0:
        return max(0.0, S - K)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return S * norm_cdf(d1) - K * math.exp(-r * T) * norm_cdf(d2)


def bs_call_delta(S, K, T, r, sigma) -> float:
    if T <= 0:
        return 1.0 if S > K else 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    return norm_cdf(d1)


def implied_vol_call(price, S, K, T, r) -> float | None:
    """Bisection, not scipy.brentq (Alpaca's own reference notebook uses
    brentq; bisection avoids adding scipy as a dependency here, matching
    gpc_wheel_bot's minimal-dependency footprint)."""
    intrinsic = max(0.0, S - K)
    if price <= intrinsic + 1e-6 or T <= 0:
        return None
    lo, hi = 1e-4, 5.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if bs_call(S, K, T, r, mid) > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


# --------------------------------------------------------------------------- #
# Market data helpers
# --------------------------------------------------------------------------- #
def get_current_price(symbol: str) -> float:
    """Latest TRADE price, not a bid/ask quote midpoint. A quote midpoint
    breaks badly once the market is closed and one side of the book comes
    back 0/stale -- e.g. bid=$208, ask=$0 averages to $104, roughly half
    the real price (this is what happened on the first live run of this
    bot). Alpaca's own reference notebook for this exact strategy uses the
    latest trade for the underlying for the same reason -- matching that
    here rather than gpc_wheel_bot's quote-midpoint convention, which is
    fine for GPC's actively-quoted shares but not safe for a closed market."""
    req = StockLatestTradeRequest(symbol_or_symbols=symbol)
    trade = stock_data_client.get_stock_latest_trade(req)[symbol]
    return float(trade.price)


def get_daily_closes(symbol: str, days: int = 150) -> list[tuple[date, float]]:
    today = date.today()
    req = StockBarsRequest(
        symbol_or_symbols=[symbol],
        timeframe=TimeFrame(amount=1, unit=TimeFrameUnit.Day),
        start=datetime.combine(today - timedelta(days=days), datetime.min.time()),
    )
    bars = stock_data_client.get_stock_bars(req).data.get(symbol, [])
    return [(b.timestamp.date(), float(b.close)) for b in bars]


def compute_ema(closes: list[float], length: int) -> list[float | None]:
    alpha = 2 / (length + 1)
    ema: list[float | None] = [None] * len(closes)
    if len(closes) < length:
        return ema
    seed = sum(closes[:length]) / length
    ema[length - 1] = seed
    for i in range(length, len(closes)):
        ema[i] = closes[i] * alpha + ema[i - 1] * (1 - alpha)
    return ema


def entry_signal(closes: list[float]) -> bool:
    ema = compute_ema(closes, EMA_LEN)
    n = len(closes)
    if n < EMA_LEN + SIGNAL_DAYS:
        return False
    for k in range(SIGNAL_DAYS):
        i = n - 1 - k
        if ema[i] is None or closes[i] <= ema[i]:
            return False
    return True


def in_earnings_week(d: date) -> date | None:
    for ed in EARNINGS_DATES:
        week_start = ed - timedelta(days=ed.weekday())
        week_end = week_start + timedelta(days=4)
        if week_start <= d <= week_end:
            return ed
    return None


def get_option_quote_mid(symbol: str) -> float | None:
    req = OptionLatestQuoteRequest(symbol_or_symbols=symbol)
    quote = option_data_client.get_option_latest_quote(req).get(symbol)
    if quote is None or quote.bid_price is None or quote.ask_price is None:
        return None
    if quote.bid_price <= 0 or quote.ask_price <= 0:
        return None
    return (quote.bid_price + quote.ask_price) / 2.0


# --------------------------------------------------------------------------- #
# Contract selection
# --------------------------------------------------------------------------- #
def parse_occ_symbol(occ_symbol: str) -> dict:
    """Parse an OCC-format option symbol, e.g. NVDA261016C00240000, into its
    underlying/expiration/right/strike parts. Assumes the underlying has no
    digits in it (true for NVDA)."""
    i = 0
    while i < len(occ_symbol) and not occ_symbol[i].isdigit():
        i += 1
    underlying = occ_symbol[:i]
    rest = occ_symbol[i:]
    exp = datetime.strptime(rest[:6], "%y%m%d").date()
    right = rest[6]
    strike = int(rest[7:]) / 1000.0
    return {"underlying": underlying, "expiration": exp, "right": right, "strike": strike}


def list_candidate_calls(spot: float):
    today = date.today()
    req = GetOptionContractsRequest(
        underlying_symbols=[SYMBOL],
        status=AssetStatus.ACTIVE,
        type=ContractType.CALL,
        strike_price_gte=str(spot * (1 - STRIKE_SEARCH_RANGE)),
        strike_price_lte=str(spot * (1 + STRIKE_SEARCH_RANGE)),
        expiration_date_gte=(today + timedelta(days=DTE_TARGET - DTE_WINDOW)).isoformat(),
        expiration_date_lte=(today + timedelta(days=DTE_TARGET + DTE_WINDOW)).isoformat(),
    )
    return trade_client.get_option_contracts(req).option_contracts


def select_spread(spot: float):
    """Returns (long_contract, long_quote, short_contract, short_quote, debit)
    for the best construction found, or None if no valid pair exists (thin
    chain, no liquid quotes, etc -- logged by the caller)."""
    contracts = list_candidate_calls(spot)
    by_expiration: dict[date, list] = {}
    for c in contracts:
        oi = float(c.open_interest) if c.open_interest is not None else 0.0
        if oi <= OI_THRESHOLD:
            continue
        exp = c.expiration_date if isinstance(c.expiration_date, date) else \
            datetime.strptime(c.expiration_date, "%Y-%m-%d").date()
        by_expiration.setdefault(exp, []).append(c)

    best = None  # (delta_diff, long_c, long_mid, short_c, short_mid, debit)
    today = date.today()
    for exp, group in by_expiration.items():
        T = max((exp - today).days, 1) / 365.0
        priced = []
        for c in group:
            mid = get_option_quote_mid(c.symbol)
            if mid is None:
                continue
            iv = implied_vol_call(mid, spot, float(c.strike_price), T, RISK_FREE_RATE)
            if iv is None:
                continue
            delta = bs_call_delta(spot, float(c.strike_price), T, RISK_FREE_RATE, iv)
            priced.append((c, mid, delta))
        if not priced:
            continue
        # long leg: closest delta to target
        long_c, long_mid, long_delta = min(priced, key=lambda t: abs(t[2] - TARGET_DELTA))
        target_short_strike = float(long_c.strike_price) + WIDTH
        shorts = [(c, mid) for c, mid, _ in priced if c.symbol != long_c.symbol]
        if not shorts:
            continue
        short_c, short_mid = min(shorts, key=lambda t: abs(float(t[0].strike_price) - target_short_strike))
        debit = long_mid - short_mid
        diff = abs(long_delta - TARGET_DELTA)
        if best is None or diff < best[0]:
            best = (diff, long_c, long_mid, short_c, short_mid, debit)

    if best is None:
        return None
    _, long_c, long_mid, short_c, short_mid, debit = best
    return long_c, long_mid, short_c, short_mid, debit


def check_entry_rules(long_c, short_c, debit: float) -> tuple[bool, str]:
    if debit <= 0:
        return False, f"non-positive debit ({debit:.2f}), quotes look wrong -- skipping"
    width = float(short_c.strike_price) - float(long_c.strike_price)
    if width <= 0:
        return False, f"short strike {short_c.strike_price} not above long strike {long_c.strike_price}"
    ratio = debit / width
    reward_risk = (width - debit) / debit
    if ratio > MAX_DEBIT_RATIO or debit > MAX_DEBIT_DOLLARS:
        return False, f"debit ${debit:.2f} is {ratio:.1%} of ${width:.0f} width -- over the {MAX_DEBIT_RATIO:.0%}/${MAX_DEBIT_DOLLARS:.2f} cap"
    if reward_risk < MIN_REWARD_RISK:
        return False, f"reward:risk {reward_risk:.2f}x is below the {MIN_REWARD_RISK}x floor"
    return True, f"debit ${debit:.2f} ({ratio:.1%} of ${width:.0f} width), reward:risk {reward_risk:.2f}x"


# --------------------------------------------------------------------------- #
# Account state
# --------------------------------------------------------------------------- #
def get_open_nvda_legs():
    """Returns (long_position_or_None, short_position_or_None) among NVDA
    option positions, keyed off Position.side (confirmed against the real
    alpaca-py model -- see module docstring)."""
    long_pos, short_pos = None, None
    for p in trade_client.get_all_positions():
        if p.symbol.startswith(SYMBOL) and len(p.symbol) > len(SYMBOL) + 6:
            if p.side == PositionSide.SHORT:
                short_pos = p
            else:
                long_pos = p
    return long_pos, short_pos


def get_open_nvda_option_order():
    req = GetOrdersRequest(status=QueryOrderStatus.OPEN)
    for o in trade_client.get_orders(req):
        if o.symbol and o.symbol.startswith(SYMBOL) and len(o.symbol) > len(SYMBOL) + 6:
            return o
        if o.legs:
            for leg in o.legs:
                if leg.symbol.startswith(SYMBOL) and len(leg.symbol) > len(SYMBOL) + 6:
                    return o
    return None


# --------------------------------------------------------------------------- #
# Order submission
# --------------------------------------------------------------------------- #
def submit_entry(long_symbol: str, short_symbol: str, limit_debit: float):
    legs = [
        OptionLegRequest(symbol=long_symbol, side=OrderSide.BUY, ratio_qty=1),
        OptionLegRequest(symbol=short_symbol, side=OrderSide.SELL, ratio_qty=1),
    ]
    req = LimitOrderRequest(
        qty=1,
        order_class=OrderClass.MLEG,
        time_in_force=TimeInForce.DAY,
        limit_price=round(limit_debit, 2),
        legs=legs,
    )
    return trade_client.submit_order(req)


def close_spread(long_symbol: str, short_symbol: str):
    """Two separate close_position calls -- Alpaca's own documented pattern
    for exiting a multi-leg position (see alpacahq/alpaca-py examples/
    options/options-bull-call-spread.ipynb, roll_rinse_bull_call_spread)."""
    results = {}
    results["short"] = trade_client.close_position(
        symbol_or_asset_id=short_symbol, close_options=ClosePositionRequest(qty="1"))
    results["long"] = trade_client.close_position(
        symbol_or_asset_id=long_symbol, close_options=ClosePositionRequest(qty="1"))
    return results


# --------------------------------------------------------------------------- #
# Main state machine
# --------------------------------------------------------------------------- #
def run():
    spot = get_current_price(SYMBOL)
    open_order = get_open_nvda_option_order()
    long_pos, short_pos = get_open_nvda_legs()
    log({"action": "check", "symbol": SYMBOL,
         "reason": f"spot={spot:.2f} long={long_pos.symbol if long_pos else None} "
                   f"short={short_pos.symbol if short_pos else None} "
                   f"open_order={open_order.id if open_order else None}"})

    # ---- A pending order already exists: never stack another on top -------
    if open_order is not None:
        log({"action": "wait", "symbol": SYMBOL,
             "reason": f"order {open_order.id} still open (status={open_order.status}), not submitting"})
        return

    # ---- Exactly one leg open: unexpected partial state, don't touch ------
    if (long_pos is None) != (short_pos is None):
        which = long_pos.symbol if long_pos else short_pos.symbol
        log({"action": "alert", "symbol": which,
             "reason": "only one leg of the spread is open -- unexpected state, check manually, not touching it"})
        return

    # ---- Spread open on both legs: manage it -------------------------------
    if long_pos is not None and short_pos is not None:
        long_mid = get_option_quote_mid(long_pos.symbol)
        short_mid = get_option_quote_mid(short_pos.symbol)
        entry_debit = float(long_pos.avg_entry_price) - float(short_pos.avg_entry_price)
        parsed = parse_occ_symbol(long_pos.symbol)
        dte = (parsed["expiration"] - date.today()).days

        if long_mid is None or short_mid is None or entry_debit <= 0:
            log({"action": "alert", "symbol": long_pos.symbol,
                 "reason": f"couldn't get live quotes or entry_debit looks wrong (long_mid={long_mid} "
                           f"short_mid={short_mid} entry_debit={entry_debit}) -- check manually"})
            return

        current_value = long_mid - short_mid
        ret = current_value / entry_debit - 1.0

        if ret >= PROFIT_TARGET or dte <= CLOSE_BY_DTE:
            reason = "profit_target" if ret >= PROFIT_TARGET else "close_by_expiration"
            close_spread(long_pos.symbol, short_pos.symbol)
            log({"action": "close_spread", "symbol": long_pos.symbol, "qty": 1,
                 "reason": f"{reason}: ret={ret:+.1%} dte={dte} entry_debit={entry_debit:.2f} current_value={current_value:.2f}"})
            return

        log({"action": "wait", "symbol": long_pos.symbol,
             "reason": f"holding: ret={ret:+.1%} (target {PROFIT_TARGET:.0%}) dte={dte} "
                       f"entry_debit={entry_debit:.2f} current_value={current_value:.2f}"})
        return

    # ---- No position: check whether to enter one ---------------------------
    today = date.today()
    blocked_by = in_earnings_week(today)
    if blocked_by:
        log({"action": "wait", "symbol": SYMBOL, "reason": f"inside earnings blackout week of {blocked_by}"})
        return

    closes_with_dates = get_daily_closes(SYMBOL)
    closes = [c for _, c in closes_with_dates]
    if not entry_signal(closes):
        log({"action": "wait", "symbol": SYMBOL,
             "reason": f"no entry signal (need {SIGNAL_DAYS} consecutive closes above the {EMA_LEN}-EMA)"})
        return

    selection = select_spread(spot)
    if selection is None:
        log({"action": "skip_entry", "symbol": SYMBOL,
             "reason": "signal fired but no valid construction found in the chain (thin liquidity or no quotes)"})
        return

    long_c, long_mid, short_c, short_mid, debit = selection
    ok, reason = check_entry_rules(long_c, short_c, debit)
    if not ok:
        log({"action": "skip_entry", "symbol": SYMBOL, "reason": reason})
        return

    order = submit_entry(long_c.symbol, short_c.symbol, debit)
    log({"action": "open_spread", "symbol": long_c.symbol, "qty": 1,
         "reason": f"long={long_c.symbol}({long_mid:.2f}) short={short_c.symbol}({short_mid:.2f}) "
                   f"debit={debit:.2f} order_id={order.id}"})


if __name__ == "__main__":
    run()
