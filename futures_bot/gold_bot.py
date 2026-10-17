"""Gold London breakout, forward test on moomoo PAPER futures.

Same rules as gold_london() in backtest.py, on 1-hour MGC bars (ET):
  1. Asian range = high/low of the 18:00-02:00 bars (session starts 18:00 ET).
  2. From the 03:00 bar to the 09:00 bar, a bar CLOSE above the range high
     buys 1 MGC; a close below the range low sells 1 short. One trade a day.
  3. Stop: a bar close back through the opposite side of the range.
  4. Time exit: flat once the 11:00 bar has closed.

Run every 15 minutes (Task Scheduler). Each run looks at the latest completed
bar, compares with the broker position, and does at most one thing.

Safety:
  - Trades only account ACC_ID and only with TrdEnv.SIMULATE; it refuses to
    run if that account is not a paper account. There is no live mode.
  - Never holds more than QTY contracts; skips while one of its orders is working.
  - Orders carry the remark "goldlondon" so they can be told apart.

Usage:
  python gold_bot.py             one check, act if needed (what the scheduler runs)
  python gold_bot.py --dry-run   one check, log what it would do, place nothing
  python gold_bot.py --status    show range, position, and today's state
  python gold_bot.py --replay    check the bot's rules reproduce the backtest
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

HERE = Path(__file__).parent
ET = ZoneInfo("America/New_York")

YF_SYMBOL = "MGC=F"
CODE = "US.MGCmain"  # moomoo maps this to the current front month
ACC_ID = 2716771  # moomoo US futures PAPER account
QTY = 1
REMARK = "goldlondon"

RANGE_HOURS = [18, 19, 20, 21, 22, 23, 0, 1, 2]
ENTRY_HOURS = [3, 4, 5, 6, 7, 8, 9]
EXIT_HOUR = 11  # flat once this bar has closed
MIN_RANGE_BARS = 7  # skip the day if the Asian range has gaps (holidays, bad data)
DATA_DELAY_MIN = 12  # Yahoo futures bars are ~10 min delayed; wait before trusting a bar
LIMIT_BUFFER = 0.005  # orders are limits 0.5% (~$20 on gold) past the last close
TICK = 0.1

STATE = HERE / "gold_state.json"
LOG = HERE / "gold_log.jsonl"
TRADES = HERE / "gold_trades.csv"


# ---------------------------------------------------------------- rules


def session_of(t: datetime) -> date:
    """CME day starting 18:00 ET belongs to the next calendar date."""
    return (t + timedelta(hours=6)).date()


def completed(bars: pd.DataFrame, now: datetime) -> pd.DataFrame:
    """Bars whose hour has ended and whose data has had time to arrive."""
    cutoff = now - timedelta(hours=1, minutes=DATA_DELAY_MIN)
    return bars[bars.index <= cutoff]


def asian_range(bars: pd.DataFrame, sess: date) -> tuple[float, float, int] | None:
    in_sess = bars[[session_of(t) == sess for t in bars.index]]
    rng = in_sess[in_sess.index.hour.isin(RANGE_HOURS)]
    if rng.empty:
        return None
    return float(rng.High.max()), float(rng.Low.min()), len(rng)


def decide(bars: pd.DataFrame, now: datetime, pos: int, traded: bool) -> tuple[str | None, str, dict]:
    """Pure decision. `bars` = completed bars only. Returns (action, reason,
    info) where action is BUY, SELL, CLOSE or None."""
    sess = session_of(now)
    info: dict = {"session": str(sess)}
    r = asian_range(bars, sess)
    if r:
        info.update(range_high=r[0], range_low=r[1], range_bars=r[2])
    last = bars.iloc[-1] if len(bars) else None
    last_t = bars.index[-1] if len(bars) else None
    if last is not None:
        info.update(last_bar=last_t.strftime("%Y-%m-%d %H:%M"), last_close=float(last.Close))

    if pos != 0:
        # entries happen 03:00-10:xx, so any position outside 03:00-12:00 is
        # either past the time exit or left over from an earlier session
        if not (ENTRY_HOURS[0] <= now.hour <= EXIT_HOUR):
            return "CLOSE", f"time exit ({EXIT_HOUR}:00 bar closed) or leftover position", info
        if r and last is not None and session_of(last_t) == sess:
            hi, lo, _ = r
            if pos > 0 and last.Close <= lo:
                return "CLOSE", f"stop: close {last.Close:.1f} <= range low {lo:.1f}", info
            if pos < 0 and last.Close >= hi:
                return "CLOSE", f"stop: close {last.Close:.1f} >= range high {hi:.1f}", info
        return None, "holding", info

    if sess.weekday() >= 5:
        return None, "weekend", info
    if traded:
        return None, "already traded this session", info
    if not r or r[2] < MIN_RANGE_BARS:
        return None, f"Asian range incomplete ({r[2] if r else 0}/{len(RANGE_HOURS)} bars)", info
    if last is None or session_of(last_t) != sess or last_t.hour not in ENTRY_HOURS:
        return None, "outside entry window (03:00-09:00 bars)", info
    hi, lo, _ = r
    if last.Close > hi:
        return "BUY", f"breakout: close {last.Close:.1f} > range high {hi:.1f}", info
    if last.Close < lo:
        return "SELL", f"breakdown: close {last.Close:.1f} < range low {lo:.1f}", info
    return None, "inside range", info


# ---------------------------------------------------------------- data


def fetch_bars(period: str = "5d") -> pd.DataFrame:
    import yfinance as yf

    d = yf.download(YF_SYMBOL, interval="60m", period=period, progress=False, auto_adjust=False)
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = d.columns.get_level_values(0)
    d = d[["Open", "High", "Low", "Close"]].dropna()
    d.index = pd.to_datetime(d.index, utc=True).tz_convert(ET)
    return d


# ---------------------------------------------------------------- broker


class Broker:
    def __init__(self):
        from moomoo import OpenFutureTradeContext, SecurityFirm

        self.ctx = OpenFutureTradeContext(host="127.0.0.1", port=11111, security_firm=SecurityFirm.FUTUINC)

    def close(self):
        self.ctx.close()

    def check_paper(self):
        from moomoo import RET_OK

        ret, accs = self.ctx.get_acc_list()
        if ret != RET_OK:
            raise RuntimeError(f"get_acc_list failed: {accs}")
        row = accs[accs.acc_id == ACC_ID]
        if row.empty or row.iloc[0].trd_env != "SIMULATE":
            raise RuntimeError(f"account {ACC_ID} is not a moomoo PAPER account; refusing to trade")

    def position(self) -> int:
        from moomoo import RET_OK, TrdEnv

        ret, p = self.ctx.position_list_query(trd_env=TrdEnv.SIMULATE, acc_id=ACC_ID)
        if ret != RET_OK:
            raise RuntimeError(f"position query failed: {p}")
        p = p[p.code.str.startswith("US.MGC") & (p.qty != 0)]
        signed = 0
        for _, r in p.iterrows():
            q = int(abs(r.qty))
            signed += -q if str(r.position_side).upper() == "SHORT" else q
        return signed

    def working_orders(self) -> int:
        from moomoo import RET_OK, TrdEnv

        ret, o = self.ctx.order_list_query(trd_env=TrdEnv.SIMULATE, acc_id=ACC_ID)
        if ret != RET_OK:
            raise RuntimeError(f"order query failed: {o}")
        live = {"SUBMITTING", "SUBMITTED", "WAITING_SUBMIT", "FILLED_PART"}
        return int((o.code.str.startswith("US.MGC") & o.order_status.isin(live)).sum())

    def marketable_limit(self, side: str, qty: int, ref_price: float) -> dict:
        """Limit order LIMIT_BUFFER past the reference price: fills like a
        market order but caps the fill. moomoo's docs don't confirm market
        orders on futures paper, and our price is ~10 min delayed. Cancelled
        if not filled within ~15 s."""
        from moomoo import RET_OK, ModifyOrderOp, OrderType, TrdEnv, TrdSide

        mult = 1 + LIMIT_BUFFER if side == "BUY" else 1 - LIMIT_BUFFER
        limit = round(round(ref_price * mult / TICK) * TICK, 1)
        ret, o = self.ctx.place_order(
            price=limit, qty=qty, code=CODE,
            trd_side=TrdSide.BUY if side == "BUY" else TrdSide.SELL,
            order_type=OrderType.NORMAL, trd_env=TrdEnv.SIMULATE, acc_id=ACC_ID, remark=REMARK,
        )
        if ret != RET_OK:
            raise RuntimeError(f"place_order failed: {o}")
        oid = o.iloc[0].order_id
        done = ("FILLED_ALL", "CANCELLED_ALL", "FAILED", "DISABLED", "DELETED")
        r = o.iloc[0]
        for _ in range(15):
            time.sleep(1)
            ret, q = self.ctx.order_list_query(order_id=oid, trd_env=TrdEnv.SIMULATE, acc_id=ACC_ID)
            if ret == RET_OK and len(q):
                r = q.iloc[0]
                if r.order_status in done:
                    break
        if r.order_status not in done:
            self.ctx.modify_order(ModifyOrderOp.CANCEL, oid, 0, 0, trd_env=TrdEnv.SIMULATE, acc_id=ACC_ID)
        return {"order_id": str(oid), "code": str(r.code), "limit": limit, "status": str(r.order_status),
                "filled_qty": float(r.get("dealt_qty", 0) or 0), "fill_price": float(r.get("dealt_avg_price", 0) or 0)}


# ---------------------------------------------------------------- state / logs


def load_state(sess: date) -> dict:
    try:
        s = json.loads(STATE.read_text())
    except (OSError, ValueError):
        s = {}
    if s.get("session") != str(sess):
        s = {"session": str(sess), "traded": False}
    return s


def log(rec: dict) -> None:
    rec = {"ts": datetime.now(ET).isoformat(timespec="seconds"), **rec}
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print(json.dumps(rec))


def record_trade(row: dict) -> None:
    new = not TRADES.exists()
    with TRADES.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow(row)


# ---------------------------------------------------------------- main


def run_once(dry: bool) -> None:
    now = datetime.now(ET)
    sess = session_of(now)
    state = load_state(sess)
    bars = completed(fetch_bars(), now)
    broker = Broker()
    try:
        broker.check_paper()
        pos = broker.position()
        if broker.working_orders():
            log({"action": None, "reason": "an MGC order is still working; waiting", "pos": pos})
            return
        action, reason, info = decide(bars, now, pos, state["traded"])
        if action is None:
            log({"action": None, "reason": reason, "pos": pos, **info})
            return
        side = {"BUY": "BUY", "SELL": "SELL", "CLOSE": "SELL" if pos > 0 else "BUY"}[action]
        qty = abs(pos) if action == "CLOSE" else QTY
        if dry:
            log({"action": action, "dry_run": True, "would": f"market {side} {qty}", "reason": reason, "pos": pos, **info})
            return
        if action in ("BUY", "SELL"):
            state["traded"] = True  # set before sending, so a crash can't cause a second entry
            STATE.write_text(json.dumps(state, indent=1))
        fill = broker.marketable_limit(side, qty, info["last_close"])
        if action in ("BUY", "SELL") and fill["filled_qty"] == 0:
            state["traded"] = False  # nothing filled: allow a retry on the next run
            reason += " (order not filled, cancelled)"
        log({"action": action, "side": side, "qty": qty, "reason": reason, "pos_before": pos, **fill, **info})
        record_trade({"time": now.isoformat(timespec="seconds"), "session": str(sess), "action": action,
                      "side": side, "qty": qty, "fill_price": fill["fill_price"],
                      "signal_close": info.get("last_close"), "range_high": info.get("range_high"),
                      "range_low": info.get("range_low"), "status": fill["status"],
                      "order_id": fill["order_id"], "reason": reason})
    finally:
        STATE.write_text(json.dumps(state, indent=1))
        broker.close()


def status() -> None:
    now = datetime.now(ET)
    bars = completed(fetch_bars(), now)
    broker = Broker()
    try:
        broker.check_paper()
        pos = broker.position()
    finally:
        broker.close()
    state = load_state(session_of(now))
    action, reason, info = decide(bars, now, pos, state["traded"])
    print(json.dumps({"now": now.isoformat(timespec="minutes"), "position": pos, "state": state,
                      "next_action": action, "reason": reason, **info}, indent=1))


def replay() -> None:
    """Walk the cached backtest data hour by hour through decide() and compare
    the resulting position with backtest.gold_london()."""
    sys.path.insert(0, str(HERE))
    import backtest

    df = backtest.load("MGC", False)
    target = backtest.gold_london(df)
    pos, traded, sess_seen, out = 0, False, None, []
    for i, t in enumerate(df.index):
        now = t + timedelta(hours=1, minutes=DATA_DELAY_MIN)
        if session_of(now) != sess_seen:
            sess_seen, traded = session_of(now), False
        action, _, _ = decide(df.iloc[max(0, i - 30): i + 1], now, pos, traded)  # a session is < 24 bars
        if action in ("BUY", "SELL"):
            pos, traded = (1 if action == "BUY" else -1), True
        elif action == "CLOSE":
            pos = 0
        out.append(pos)
        if i % 2000 == 0:
            print(f"  {i}/{len(df)}", flush=True)
    bot = pd.Series(out, index=df.index, dtype=float)
    match = (bot == target).mean()
    pnl_bt, _ = backtest.simulate(df, target, backtest.SPECS["MGC"])
    pnl_bot, _ = backtest.simulate(df, bot, backtest.SPECS["MGC"])
    print(f"bars where bot position == backtest position: {match:.1%}")
    print(f"backtest net ${pnl_bt.sum():,.0f}  vs  bot rules net ${pnl_bot.sum():,.0f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--status", action="store_true")
    g.add_argument("--replay", action="store_true")
    a = ap.parse_args()
    try:
        if a.status:
            status()
        elif a.replay:
            replay()
        else:
            run_once(a.dry_run)
    except Exception as e:  # scheduler runs headless: make every failure visible in the log
        log({"action": "error", "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-1500:]})
        sys.exit(1)


if __name__ == "__main__":
    main()
