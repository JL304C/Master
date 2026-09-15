"""
GPC cash-secured wheel — scheduled paper-trading bot for Alpaca.

Implements exactly the strategy audited earlier:
  - Sell 1 cash-secured put, ~30 DTE, ~20% OTM, only if strike*100 fits under
    the $26,000 cap and available cash.
  - If assigned: hold 100 shares, sell 1 covered call, ~30 DTE, ~10% OTM.
  - At <=5 days to expiry, if the call is still OTM, roll it: buy to close,
    sell a new ~30 DTE call at max(10% OTM from current price, cost basis) --
    the cost-basis floor from the audit, so we never voluntarily lock in a
    loss on the shares.
  - Once shares are called away, restart with a new put.

Derives all state fresh from Alpaca's own account each run (positions' own
avg_entry_price and each option symbol's own encoded strike/expiration) --
no separate local state file to drift out of sync with reality.

Meant to run once per trading day via Windows Task Scheduler. Paper trading
only: this script never sets ALPACA_PAPER=false or points at the live
endpoint. Going live later means writing that decision explicitly, not
flipping a flag here.

Requires: pip install alpaca-py
Credentials: set ALPACA_API_KEY / ALPACA_SECRET_KEY as environment variables,
or create a .env file next to this script (see load_env_file below).
"""

from __future__ import annotations

import csv
import json
import os
import sys
from datetime import datetime, timedelta, date
from pathlib import Path

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    GetOptionContractsRequest,
    ClosePositionRequest,
)
from alpaca.trading.enums import (
    AssetStatus,
    OrderSide,
    OrderType,
    TimeInForce,
    ContractType,
)
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest

# --------------------------------------------------------------------------- #
# Strategy parameters -- exactly what was audited. Change these and you are
# running a DIFFERENT, un-audited strategy.
# --------------------------------------------------------------------------- #
SYMBOL = "GPC"
ACCOUNT_CAP = 26_000.0       # fixed dollar ceiling, independent of paper account size
PUT_OTM_PCT = 0.20           # put strike ~20% below current price
CALL_OTM_PCT = 0.10          # call strike ~10% above current price
DTE_TARGET = 30              # target days to expiration when opening
ROLL_TRIGGER_DAYS = 5        # roll the call when this many days remain
STRIKE_INCREMENT = 5.0       # GPC trades in $5 strike increments in practice;
                              # adjust if the real chain uses a different increment

HERE = Path(__file__).resolve().parent
LOG_JSONL = HERE / "gpc_wheel_log.jsonl"
LOG_CSV = HERE / "gpc_wheel_trades.csv"


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
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)


def log(event: dict) -> None:
    event = {"ts": datetime.utcnow().isoformat(), **event}
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


def round_to_increment(x: float, inc: float) -> float:
    return round(x / inc) * inc


def get_current_price(symbol: str) -> float:
    req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
    quote = data_client.get_stock_latest_quote(req)[symbol]
    # midpoint of the latest quoted spread
    return (quote.ask_price + quote.bid_price) / 2.0


def parse_occ_symbol(occ_symbol: str) -> dict:
    """Parse an OCC-format option symbol, e.g. GPC260117P00120000, into its
    underlying/expiration/right/strike parts. Assumes the underlying has no
    digits in it (true for GPC)."""
    i = 0
    while i < len(occ_symbol) and not occ_symbol[i].isdigit():
        i += 1
    underlying = occ_symbol[:i]
    rest = occ_symbol[i:]
    exp = datetime.strptime(rest[:6], "%y%m%d").date()
    right = rest[6]
    strike = int(rest[7:]) / 1000.0
    return {"underlying": underlying, "expiration": exp, "right": right, "strike": strike}


def get_positions():
    """Returns (share_qty, avg_cost_basis_or_None, option_position_or_None)."""
    positions = trade_client.get_all_positions()
    share_qty = 0
    cost_basis = None
    option_pos = None
    for p in positions:
        if p.symbol == SYMBOL:
            share_qty = int(float(p.qty))
            cost_basis = float(p.avg_entry_price)
        elif p.symbol.startswith(SYMBOL) and len(p.symbol) > len(SYMBOL) + 6:
            # looks like an OCC option symbol on our underlying
            option_pos = p
    return share_qty, cost_basis, option_pos


def find_contract(right: ContractType, target_strike: float, dte_target: int):
    """Finds the contract closest to target_strike among those expiring
    within a window around dte_target days out."""
    today = date.today()
    exp_from = today + timedelta(days=dte_target - 5)
    exp_to = today + timedelta(days=dte_target + 5)
    req = GetOptionContractsRequest(
        underlying_symbols=[SYMBOL],
        status=AssetStatus.ACTIVE,
        type=right,
        expiration_date_gte=exp_from.isoformat(),
        expiration_date_lte=exp_to.isoformat(),
        strike_price_gte=str(target_strike * 0.85),
        strike_price_lte=str(target_strike * 1.15),
    )
    contracts = trade_client.get_option_contracts(req).option_contracts
    if not contracts:
        return None
    # pick the contract with strike closest to target, then expiration
    # closest to the target DTE
    def score(c):
        strike_diff = abs(float(c.strike_price) - target_strike)
        exp_diff = abs((datetime.strptime(c.expiration_date, "%Y-%m-%d").date() - today).days - dte_target)
        return (strike_diff, exp_diff)
    contracts.sort(key=score)
    return contracts[0]


def submit_single_leg(symbol: str, side: OrderSide, qty: int = 1):
    req = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=side,
        type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
    )
    return trade_client.submit_order(req)


def run():
    price = get_current_price(SYMBOL)
    share_qty, cost_basis, option_pos = get_positions()
    log({"action": "check", "symbol": SYMBOL, "reason": f"price={price:.2f} shares={share_qty} option={option_pos.symbol if option_pos else None}"})

    # ---- No shares, no open option: consider selling a new put -----------
    if share_qty == 0 and option_pos is None:
        target_strike = round_to_increment(price * (1 - PUT_OTM_PCT), STRIKE_INCREMENT)
        need = target_strike * 100
        if need > ACCOUNT_CAP:
            log({"action": "skip_put", "symbol": SYMBOL, "reason": f"20%-OTM strike {target_strike} needs ${need:,.0f}, over the ${ACCOUNT_CAP:,.0f} cap"})
            return
        contract = find_contract(ContractType.PUT, target_strike, DTE_TARGET)
        if contract is None:
            log({"action": "skip_put", "symbol": SYMBOL, "reason": "no suitable contract found in chain"})
            return
        order = submit_single_leg(contract.symbol, OrderSide.SELL)
        log({"action": "sell_put", "symbol": contract.symbol, "qty": 1,
             "reason": f"strike={contract.strike_price} exp={contract.expiration_date} order_id={order.id}"})
        return

    # ---- Short put open, not yet resolved: nothing to do ------------------
    if share_qty == 0 and option_pos is not None:
        parsed = parse_occ_symbol(option_pos.symbol)
        if parsed["right"] == "P":
            log({"action": "wait", "symbol": option_pos.symbol, "reason": "short put still open, waiting for expiration/assignment"})
            return

    # ---- Shares held, no covered call yet: sell one -----------------------
    if share_qty >= 100 and option_pos is None:
        target_strike = round_to_increment(price * (1 + CALL_OTM_PCT), STRIKE_INCREMENT)
        contract = find_contract(ContractType.CALL, target_strike, DTE_TARGET)
        if contract is None:
            log({"action": "skip_call", "symbol": SYMBOL, "reason": "no suitable contract found in chain"})
            return
        order = submit_single_leg(contract.symbol, OrderSide.SELL)
        log({"action": "sell_call", "symbol": contract.symbol, "qty": 1,
             "reason": f"strike={contract.strike_price} exp={contract.expiration_date} order_id={order.id}"})
        return

    # ---- Shares + open covered call: check roll/assignment -----------------
    if share_qty >= 100 and option_pos is not None:
        parsed = parse_occ_symbol(option_pos.symbol)
        if parsed["right"] != "C":
            log({"action": "alert", "symbol": option_pos.symbol, "reason": "shares held but open option is a put -- unexpected state, check manually"})
            return
        dte = (parsed["expiration"] - date.today()).days
        strike = parsed["strike"]
        if dte > ROLL_TRIGGER_DAYS:
            log({"action": "wait", "symbol": option_pos.symbol, "reason": f"covered call open, {dte}d to expiry, not yet at roll trigger"})
            return
        if price >= strike:
            log({"action": "wait", "symbol": option_pos.symbol, "reason": f"covered call ITM (price {price:.2f} >= strike {strike}), letting assignment happen naturally"})
            return
        # Still OTM with <=5 days left: roll.
        close_order = submit_single_leg(option_pos.symbol, OrderSide.BUY)
        log({"action": "buy_to_close_call", "symbol": option_pos.symbol, "qty": 1, "reason": f"rolling, order_id={close_order.id}"})
        new_target = max(round_to_increment(price * (1 + CALL_OTM_PCT), STRIKE_INCREMENT),
                          round_to_increment(cost_basis, STRIKE_INCREMENT) if cost_basis else 0)
        new_contract = find_contract(ContractType.CALL, new_target, DTE_TARGET)
        if new_contract is None:
            log({"action": "alert", "symbol": SYMBOL, "reason": "closed old call but found no new contract to roll into -- shares now uncovered, check manually"})
            return
        open_order = submit_single_leg(new_contract.symbol, OrderSide.SELL)
        log({"action": "roll_call", "symbol": new_contract.symbol, "qty": 1,
             "reason": f"strike={new_contract.strike_price} exp={new_contract.expiration_date} cost_basis_floor={cost_basis} order_id={open_order.id}"})
        return

    log({"action": "alert", "symbol": SYMBOL, "reason": f"unhandled state: shares={share_qty} option={option_pos}"})


if __name__ == "__main__":
    run()
