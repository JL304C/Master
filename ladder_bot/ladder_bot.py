"""
1-1-1-2 Put Step-Down Ladder -- Alpaca PAPER-trading bot (SPY or XSP options).

Underlying: LADDER_UNDERLYING=SPY (default) or XSP, or --underlying on the
command line. XSP (Mini-SPX = SPX/10) is cash-settled and European, so no
early assignment and no 100 shares delivered at expiry. Alpaca has no quote
for the index itself, so XSP's level is inferred from put-call parity.

    K1 BUY 1 ~22.5d | K2 SELL 1 ~17d | K3 BUY 1 ~13d | K4 SELL 2 ~10d
    same monthly expiration closest to 90 DTE, equal strike spacing,
    one new ladder per Friday, only for a net credit, one mleg order (1/1/1/2).

SAFETY GATES (all must pass before any order is sent):
  1. APPROVAL: orders are only submitted when ladder_bot/.env contains
     LADDER_BACKTEST_APPROVED=yes. Without it the bot runs in DRY-RUN: it
     builds the ladder from the live chain, prints the math and the exact
     order it WOULD send, and logs it -- nothing is submitted.
  2. paper=True is hard-coded.
  3. Buying-power cap: the strategy's total cash-secured requirement (open
     ladders + the new one) must stay under LADDER_BP_CAP_PCT (default 20%)
     of account equity, and under Alpaca's options_buying_power.
  4. Account must report options_trading_level >= 3.
  5. No new ladder if an option order (SPY/XSP or any mleg) is still working.

MANAGEMENT (LADDER_MODE):
  hold (default) - the backtested baseline: no target, no stop, hold to expiry.
  stop           - also run daily: close a whole ladder (one mleg market
                   order) if the underlying trades below that ladder's lower breakeven.

Run daily via Task Scheduler (e.g. 3:30 PM ET): opens on Fridays, checks
stops every day in stop mode. State comes from Alpaca positions plus the
ledger ladder_ledger.jsonl (strikes/credit/breakeven of each ladder opened).

Requires: pip install alpaca-py
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import ladder_common as lc

HERE = Path(__file__).resolve().parent
LOG_JSONL = HERE / "ladder_log.jsonl"
LOG_CSV = HERE / "ladder_trades.csv"
LEDGER = HERE / "ladder_ledger.jsonl"

UNDERLYINGS = ("SPY", "XSP")   # XSP = Mini-SPX index (SPX/10): cash-settled, European, ~SPY-sized
STRIKE_INC = 1.0
RATE, DIV_YIELD = 0.04, 0.013  # for Black-Scholes fallbacks and the XSP forward


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


load_env_file(HERE / ".env")

APPROVED = os.environ.get("LADDER_BACKTEST_APPROVED", "").lower() == "yes"
BP_CAP_PCT = float(os.environ.get("LADDER_BP_CAP_PCT", "20")) / 100.0
MODE = os.environ.get("LADDER_MODE", "hold").lower()
SYMBOL = os.environ.get("LADDER_UNDERLYING", "SPY").upper()   # overridden by --underlying


def log(event: dict) -> None:
    event = {"ts": datetime.now(timezone.utc).isoformat(), "dry_run": not APPROVED, **event}
    with LOG_JSONL.open("a") as fh:
        fh.write(json.dumps(event, default=str) + "\n")
    is_new = not LOG_CSV.exists()
    with LOG_CSV.open("a", newline="") as fh:
        w = csv.writer(fh)
        if is_new:
            w.writerow(["timestamp", "dry_run", "action", "expiration", "strikes", "credit",
                        "max_profit", "breakeven", "bp_required", "order_id", "reason"])
        w.writerow([event["ts"], event["dry_run"], event.get("action", ""), event.get("expiration", ""),
                    event.get("strikes", ""), event.get("credit", ""), event.get("max_profit", ""),
                    event.get("breakeven", ""), event.get("bp_required", ""), event.get("order_id", ""),
                    event.get("reason", "")])
    print(json.dumps(event, indent=2, default=str))


def occ(exp: date, k: float) -> str:
    return f"{SYMBOL}{exp:%y%m%d}P{int(round(k * 1000)):08d}"


def parse_occ(sym: str):
    """XSP270115P00740000 -> (expiration, "P", 740.0). Works for any root."""
    i = next(n for n, ch in enumerate(sym) if ch.isdigit())
    return datetime.strptime(sym[i:i + 6], "%y%m%d").date(), sym[i + 6], int(sym[i + 7:]) / 1000.0


class Alpaca:
    def __init__(self):
        from alpaca.trading.client import TradingClient
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.historical.option import OptionHistoricalDataClient
        key, sec = os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY")
        if not key or not sec:
            sys.exit("Missing ALPACA_API_KEY / ALPACA_SECRET_KEY (env var or .env next to this script).")
        self.trade = TradingClient(key, sec, paper=True)       # paper only, on purpose
        self.stock = StockHistoricalDataClient(key, sec)
        self.opt = OptionHistoricalDataClient(key, sec)

    def stock_mid(self, sym: str) -> float:
        from alpaca.data.requests import StockLatestQuoteRequest
        q = self.stock.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=sym))[sym]
        return (q.bid_price + q.ask_price) / 2.0

    def spot(self, underlying: str, exp: date) -> float:
        """SPY: the stock quote. XSP is an index with no stock quote on Alpaca,
        so its level comes from put-call parity on its own options at `exp`:
        F = K + (C - P) * e^(rT) at the strikes nearest the money, then
        spot = F * e^(-(r - q)T). SPY x ~1 only seeds the strike window."""
        if underlying == "SPY":
            return self.stock_mid("SPY")
        guess = self.stock_mid("SPY")
        lo, hi = guess * 0.92, guess * 1.08
        puts = self.chain(exp, lo, hi, underlying, right="put")
        calls = self.chain(exp, lo, hi, underlying, right="call")
        T = max((exp - date.today()).days, 1) / 365.0
        pairs = sorted((abs(calls[k]["mid"] - puts[k]["mid"]), k + (calls[k]["mid"] - puts[k]["mid"]) * math.exp(RATE * T))
                       for k in set(puts) & set(calls))
        if len(pairs) < 3:
            raise RuntimeError(f"{underlying}: too few call/put pairs quoted near the money to infer the index level")
        fwds = sorted(f for _, f in pairs[:5])
        return fwds[len(fwds) // 2] * math.exp(-(RATE - DIV_YIELD) * T)

    def chain(self, exp: date, lo: float, hi: float, underlying: str = None, right: str = "put") -> dict:
        """{strike: {"bid","ask","mid","delta","iv","symbol"}} for one side of the chain at exp."""
        from alpaca.data.requests import OptionChainRequest
        from alpaca.trading.enums import ContractType
        snaps = self.opt.get_option_chain(OptionChainRequest(
            underlying_symbol=underlying or SYMBOL,
            type=ContractType.PUT if right == "put" else ContractType.CALL, expiration_date=exp,
            strike_price_gte=lo, strike_price_lte=hi))
        out = {}
        for sym, s in snaps.items():
            q = s.latest_quote
            if q is None or not q.bid_price or not q.ask_price:
                continue
            _, _, k = parse_occ(sym)
            out[k] = {"symbol": sym, "bid": q.bid_price, "ask": q.ask_price,
                      "mid": (q.bid_price + q.ask_price) / 2.0,
                      "delta": abs(s.greeks.delta) if s.greeks and s.greeks.delta is not None else None,
                      "iv": s.implied_volatility}
        return out


def snap_equal_spacing(raw, strikes, max_shift=2):
    """Equal-spaced (K1, K2, K3, K4) using only strikes that exist in the chain.
    Far-OTM SPY strikes at ~90 DTE are often $5 apart, so the ideal spacing
    (raw K1 - raw K4) / 3 is snapped to whatever the chain allows. K4 may move
    up to `max_shift` listed strikes from the 10-delta strike; the set closest
    to the delta-chosen strikes wins."""
    have = set(strikes)
    i4 = strikes.index(raw[3])
    ideal_w = (raw[0] - raw[3]) / 3.0
    best, best_score = None, None
    for k4 in strikes[max(0, i4 - max_shift): i4 + max_shift + 1]:
        for k3 in strikes:
            w = k3 - k4
            if w <= 0 or w > 2 * ideal_w + 5:
                continue
            ks = (k4 + 3 * w, k4 + 2 * w, k4 + w, k4)
            if not all(k in have for k in ks):
                continue
            score = sum(abs(a - b) for a, b in zip(ks, raw))
            if best_score is None or score < best_score:
                best, best_score = ks, score
    return best


def pick_ladder(spot: float, exp: date, chain: dict, r=RATE, q=DIV_YIELD):
    """Delta first, then snap to equal spacing on strikes that actually exist.
    Delta source, in order: Alpaca's greeks; Black-Scholes from Alpaca's IV;
    Black-Scholes from IV solved off the quote's mid (Alpaca sends no greeks
    or IV for XSP index options)."""
    T = max((exp - date.today()).days, 1) / 365.0

    def delta(k, row):
        if row["delta"] is not None:
            return row["delta"]
        iv = row["iv"] or lc.implied_vol_put(row["mid"], spot, k, T, r, q)
        return lc.bs_put_delta(spot, k, T, r, q, iv) if iv else None

    deltas = {k: delta(k, row) for k, row in chain.items()}
    deltas = {k: d for k, d in deltas.items() if d is not None}
    if not deltas:
        return None, f"no deltas/IV in chain and no IV solvable from quotes (spot {spot:.2f}, {len(chain)} strikes)"
    raw = [min(deltas, key=lambda k: abs(deltas[k] - t)) for t in lc.TARGET_DELTAS]
    ks = snap_equal_spacing(raw, sorted(chain))
    if ks is None:
        return None, (f"no equally spaced set of quoted strikes near delta strikes {raw}; "
                      f"quoted strikes {min(chain):g}-{max(chain):g}: {sorted(chain)[:60]}")
    w = ks[0] - ks[1]
    mid_credit = -sum(rr * chain[k]["mid"] for rr, k in zip(lc.RATIOS, ks))
    natural = -sum(rr * (chain[k]["ask"] if rr > 0 else chain[k]["bid"]) for rr, k in zip(lc.RATIOS, ks))
    lad = lc.Ladder(*ks, width=w, credit=mid_credit, raw_strikes=tuple(raw))
    return lad, {"mid_credit": mid_credit, "natural_credit": natural,
                 "deltas": {k: round(deltas.get(k, float('nan')), 3) for k in ks}}


def ledger():
    if not LEDGER.exists():
        return []
    return [json.loads(l) for l in LEDGER.read_text().splitlines() if l.strip()]


def open_ladders(api):
    """Ledger ladders whose legs are still held in the account."""
    held = {p.symbol: int(float(p.qty)) for p in api.trade.get_all_positions()}
    return [L for L in ledger() if L.get("status") == "open" and any(s in held for s in L["symbols"])], held


def strategy_bp_in_use(api, held) -> float:
    """Cash-secured requirement of every SPY/XSP put held (all ladders, both
    underlyings), per root+expiration: the most those legs can lose at expiry
    (underlying -> 0), premium excluded."""
    by_exp = {}
    for sym, qty in held.items():
        root = next((u for u in UNDERLYINGS if sym.startswith(u)), None)
        if root is None or len(sym) < len(root) + 15:
            continue
        exp, right, k = parse_occ(sym)
        if right != "P":
            continue
        key = (root, exp)
        by_exp[key] = by_exp.get(key, 0.0) + qty * k     # payoff at S=0 = sum(qty*K)
    return sum(max(0.0, -v) * lc.MULTIPLIER for v in by_exp.values())


def submit_mleg(api, legs, limit_price=None):
    """legs: [(symbol, side, ratio, intent)]. One order, never separate legs."""
    from alpaca.trading.requests import OptionLegRequest, LimitOrderRequest, MarketOrderRequest
    from alpaca.trading.enums import OrderClass, TimeInForce
    leg_reqs = [OptionLegRequest(symbol=s, side=side, ratio_qty=ratio, position_intent=intent)
                for s, side, ratio, intent in legs]
    common = dict(qty=1, order_class=OrderClass.MLEG, time_in_force=TimeInForce.DAY, legs=leg_reqs)
    if limit_price is None:
        return api.trade.submit_order(MarketOrderRequest(**common))
    return api.trade.submit_order(LimitOrderRequest(limit_price=round(limit_price, 2), **common))


def try_open(api, force_day=False):
    from alpaca.trading.enums import OrderSide, PositionIntent, QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest
    today = date.today()
    if today.weekday() != 4 and not force_day:
        return
    acct = api.trade.get_account()
    level = int(getattr(acct, "options_trading_level", 0) or 0)
    if level < 3:
        log({"action": "skip_open", "reason": f"options_trading_level={level}, need 3"})
        return
    pending = [o for o in api.trade.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
               if (o.symbol or "").startswith(UNDERLYINGS) or o.order_class == "mleg"]
    if pending:
        log({"action": "skip_open", "reason": f"{len(pending)} open option order(s) still working"})
        return

    exp = lc.monthly_expiry_near(today, 90)
    try:
        spot = api.spot(SYMBOL, exp)
        chain = api.chain(exp, spot * 0.55, spot)
    except Exception as e:
        log({"action": "skip_open", "underlying": SYMBOL, "expiration": exp,
             "reason": f"could not read {SYMBOL} price/chain from Alpaca: {e}"})
        return
    if not chain:
        log({"action": "skip_open", "underlying": SYMBOL, "expiration": exp,
             "reason": f"Alpaca returned no quoted {SYMBOL} puts for {exp}"})
        return
    lad, info = pick_ladder(spot, exp, chain)
    if lad is None:
        log({"action": "skip_open", "underlying": SYMBOL, "spot": round(spot, 2), "expiration": exp, "reason": info})
        return

    # Limit at mid (negative = credit for Alpaca mleg). Must be a net credit.
    credit = round(info["mid_credit"], 2)
    if credit <= 0:
        log({"action": "skip_open", "expiration": exp, "strikes": lad.strikes, "credit": credit,
             "reason": "ladder does not open for a net credit at mid"})
        return
    lad.credit = credit
    need = lad.cash_secured_requirement()
    equity = float(acct.equity)
    _, held = open_ladders(api)
    in_use = strategy_bp_in_use(api, held)
    cap = equity * BP_CAP_PCT
    obp = float(acct.options_buying_power or 0)
    math_ = {"underlying": SYMBOL, "expiration": exp, "spot": round(spot, 2), "strikes": lad.strikes, "width": lad.width,
             "raw_delta_strikes": lad.raw_strikes, "deltas": info["deltas"],
             "credit": credit, "natural_credit": round(info["natural_credit"], 2),
             "max_profit": round(lad.max_profit(), 2), "breakeven": round(lad.breakeven(), 2),
             **lad.drop_losses(spot), "bp_required": round(need, 2),
             "strategy_bp_in_use": round(in_use, 2), "bp_cap": round(cap, 2),
             "options_buying_power": obp}
    if in_use + need > cap:
        log({"action": "skip_open", **math_,
             "reason": f"cap: in use {in_use:,.0f} + new {need:,.0f} > {BP_CAP_PCT:.0%} of equity ({cap:,.0f})"})
        return
    if need > obp:
        log({"action": "skip_open", **math_, "reason": f"options_buying_power {obp:,.0f} < {need:,.0f}"})
        return

    syms = [chain[k]["symbol"] for k in lad.strikes]
    legs = [(syms[0], OrderSide.BUY, 1, PositionIntent.BUY_TO_OPEN),
            (syms[1], OrderSide.SELL, 1, PositionIntent.SELL_TO_OPEN),
            (syms[2], OrderSide.BUY, 1, PositionIntent.BUY_TO_OPEN),
            (syms[3], OrderSide.SELL, 2, PositionIntent.SELL_TO_OPEN)]
    if not APPROVED:
        log({"action": "would_open", **math_, "legs": [(s, str(sd.value), r) for s, sd, r, _ in legs],
             "limit_price": -credit, "reason": "DRY RUN: set LADDER_BACKTEST_APPROVED=yes after approving the backtest"})
        return
    try:
        order = submit_mleg(api, legs, limit_price=-credit)
    except Exception as e:  # Alpaca rejects mlegs with an uncovered short leg -- surface it
        log({"action": "open_rejected", **math_, "reason": str(e)})
        return
    with LEDGER.open("a") as fh:
        fh.write(json.dumps({"status": "open", "underlying": SYMBOL, "opened": today.isoformat(),
                             "order_id": str(order.id),
                             "expiration": exp.isoformat(), "strikes": lad.strikes, "symbols": syms,
                             "credit": credit, "breakeven": lad.breakeven(),
                             "max_profit": lad.max_profit(), "fill_confirmed": False}) + "\n")
    log({"action": "open_ladder", **math_, "order_id": order.id, "limit_price": -credit})


def check_stops(api):
    from alpaca.trading.enums import OrderSide, PositionIntent
    ladders, held = open_ladders(api)
    if not ladders:
        return
    all_l = ledger()
    for L in ladders:
        und = L.get("underlying", "SPY")
        spot = api.spot(und, date.fromisoformat(L["expiration"]))
        if spot >= L["breakeven"]:
            continue
        s = L["symbols"]
        legs = [(s[0], OrderSide.SELL, 1, PositionIntent.SELL_TO_CLOSE),
                (s[1], OrderSide.BUY, 1, PositionIntent.BUY_TO_CLOSE),
                (s[2], OrderSide.SELL, 1, PositionIntent.SELL_TO_CLOSE),
                (s[3], OrderSide.BUY, 2, PositionIntent.BUY_TO_CLOSE)]
        base = {"expiration": L["expiration"], "strikes": L["strikes"], "breakeven": L["breakeven"],
                "reason": f"{und} {spot:.2f} below breakeven {L['breakeven']:.2f}"}
        if not APPROVED:
            log({"action": "would_stop", **base})
            continue
        order = submit_mleg(api, legs)          # market: the point of the stop is to get out
        for x in all_l:
            if x["order_id"] == L["order_id"]:
                x["status"], x["closed"] = "stopped", date.today().isoformat()
        log({"action": "stop_close", **base, "order_id": order.id})
    LEDGER.write_text("".join(json.dumps(x) + "\n" for x in all_l))


def reconcile_fills(api):
    """Replace each new ladder's limit credit with Alpaca's actual fill price
    (an mleg can fill better than its limit) and recompute breakeven and max
    profit from it. Ladders whose order died unfilled are marked "unfilled"."""
    entries = ledger()
    changed = False
    for L in entries:
        if L.get("status") != "open" or L.get("fill_confirmed") is True:   # older entries lack the key
            continue
        o = api.trade.get_order_by_id(L["order_id"])
        status = getattr(o.status, "value", str(o.status))
        if status == "filled" and o.filled_avg_price is not None:
            credit = -float(o.filled_avg_price)        # mleg: negative price = credit
            k = L["strikes"]
            lad = lc.Ladder(*k, width=k[0] - k[1], credit=credit, raw_strikes=())
            old = L["credit"]
            L.update(credit=credit, breakeven=lad.breakeven(), max_profit=lad.max_profit(),
                     limit_credit=L.get("limit_credit", old), fill_confirmed=True)
            changed = True
            log({"action": "fill_confirmed", "order_id": L["order_id"], "expiration": L["expiration"],
                 "strikes": k, "credit": credit, "max_profit": round(lad.max_profit(), 2),
                 "breakeven": round(lad.breakeven(), 2), "reason": f"filled at {credit:.2f} credit (limit was {old:.2f})"})
        elif status in ("canceled", "expired", "rejected") and not float(o.filled_qty or 0):
            L.update(status="unfilled", fill_confirmed=True)
            changed = True
            log({"action": "unfilled", "order_id": L["order_id"], "reason": f"order {status} without a fill"})
    if changed:
        LEDGER.write_text("".join(json.dumps(x) + "\n" for x in entries))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force-day", action="store_true", help="run the Friday entry logic today (testing)")
    ap.add_argument("--underlying", choices=UNDERLYINGS, help="override LADDER_UNDERLYING for this run")
    ap.add_argument("--bp-cap-pct", type=float, help="override LADDER_BP_CAP_PCT for this run")
    args = ap.parse_args()
    global SYMBOL, BP_CAP_PCT
    if args.underlying:
        SYMBOL = args.underlying
    if args.bp_cap_pct is not None:
        BP_CAP_PCT = args.bp_cap_pct / 100.0
    api = Alpaca()
    log({"action": "check", "reason": f"underlying={SYMBOL} mode={MODE} approved={APPROVED} bp_cap={BP_CAP_PCT:.0%}"})
    reconcile_fills(api)
    if MODE == "stop":
        check_stops(api)
    try_open(api, args.force_day)


if __name__ == "__main__":
    main()
