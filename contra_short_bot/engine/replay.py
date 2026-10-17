"""Replay one or more days through the strategies with a simple fill model, to
see which setups fire and check them against charts. This is NOT the backtest:
there are no profit targets, breakeven or trailing stops yet (the Trade Manager,
step 4). Positions exit on their stop, on Strategy D's trend-break rule, or at
15:55. MFE/MAE (best/worst move in R within 60 minutes of entry) show what a
target could have caught.

    python -m engine.replay --day 2024-03-08
    python -m engine.replay --start 2024-03-01 --end 2024-03-29 --charts
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import time

import numpy as np
import pandas as pd

from data.fetch import ROOT, load_config
from engine.features import build_features
from engine.trader import DayGate, SymbolTrader
from engine.types import Cancel, ExitAll, Fill, Log, MoveStop, Place
from levels.library import build_levels
from selection.context import has_history
from selection.scanners import spy_pct_change
from selection.watchlist import build_watchlist, universe


@dataclass
class Position:
    symbol: str
    strategy: str
    reason: str
    open_t: pd.Timestamp
    fills: list = field(default_factory=list)     # (time, qty, price, tag)
    stop: float | None = None
    first_stop: float | None = None
    risk_budget: float = 0.0
    exit_on_next_open: str = ""

    @property
    def qty(self) -> int:
        return sum(q for _, q, _, _ in self.fills)

    @property
    def avg(self) -> float:
        return sum(q * p for _, q, p, _ in self.fills) / self.qty


def replay_day(day, cfg: dict | None = None, equity: float | None = None, symbols: list[str] | None = None):
    cfg = cfg or load_config()
    equity = equity or cfg["account"]["equity"]
    day = pd.Timestamp(day).normalize()
    tone, entries = build_watchlist(day, cfg=cfg)
    on_list = {e.symbol for e in entries}
    strengths = [lv.strength for e in entries for lv in e.resistance(5)]
    gate = DayGate(day, tone, strengths, cfg)
    spy = spy_pct_change(day)
    traders: dict[str, SymbolTrader] = {}
    for sym in symbols or universe(cfg):
        if not has_history(sym, day):
            continue
        f = build_features(sym, day, cfg, spy_pct=spy)
        if f.empty:
            continue
        traders[sym] = SymbolTrader(sym, f, build_levels(sym, day), gate, equity, sym in on_list, cfg)

    logs, trades = [f"{day:%Y-%m-%d} premarket tone {tone}; watchlist: {', '.join(e.symbol for e in entries)}"], []
    orders: dict[int, tuple] = {}
    pos: dict[str, Position] = {}
    flatten = time.fromisoformat(cfg["session"]["flatten"])
    pos_i = {s: {t: k for k, t in enumerate(tr.f.index)} for s, tr in traders.items()}
    clock = sorted(set().union(*[tr.f.index for tr in traders.values()])) if traders else []

    def close(sym, t, px, why):
        p = pos.pop(sym)
        pnl = sum(q * (fp - px) for _, q, fp, _ in p.fills)
        f = traders[sym].f
        after = f[(f.index > p.fills[0][0]) & (f.index <= p.fills[0][0] + pd.Timedelta(minutes=60))]
        e1 = p.fills[0][2]
        # 1R = the initial risk of the shares actually held: each fill to the first stop.
        risk = sum(q * (p.first_stop - fp) for _, q, fp, _ in p.fills) if p.first_stop else 0.0
        r_unit = max((p.first_stop or e1) - e1, 1e-9)
        trades.append({
            "date": f"{day:%Y-%m-%d}", "symbol": sym, "strategy": p.strategy, "entry_time": p.fills[0][0],
            "entry": round(e1, 2), "adds": len(p.fills) - 1, "qty": p.qty, "avg": round(p.avg, 2),
            "first_stop": p.first_stop, "exit_time": t, "exit": round(px, 2), "exit_reason": why,
            "pnl": round(pnl, 2), "R": round(pnl / risk, 2) if risk > 0 else np.nan,
            "mfe_R": round((e1 - after["low"].min()) / r_unit, 2) if len(after) else np.nan,
            "mae_R": round((after["high"].max() - e1) / r_unit, 2) if len(after) else np.nan,
            "reason": p.reason})
        logs.append(f"{t:%H:%M} {sym} exit {p.qty} @ {px:.2f} ({why}), P&L {pnl:+,.0f}")
        for a in traders[sym].on_closed("stop" if why == "stop" else why):
            handle(sym, t, a)

    def handle(sym, t, a):
        if isinstance(a, Log):
            logs.append(f"{t:%H:%M} {a.symbol} {a.text}")
        elif isinstance(a, Place):
            orders[a.order.oid] = (sym, a.order)
            logs.append(f"{t:%H:%M} {sym} place {a.order.side}-stop {a.order.qty} @ {a.order.price:.2f}: {a.order.reason}")
        elif isinstance(a, Cancel):
            orders.pop(a.oid, None)
        elif isinstance(a, MoveStop):
            p = pos.get(sym)
            if p is not None:
                p.stop = a.price if p.stop is None else min(p.stop, a.price)
                p.first_stop = p.first_stop or a.price
                logs.append(f"{t:%H:%M} {sym} stop {p.stop:.2f}: {a.reason}")
        elif isinstance(a, ExitAll):
            if sym in pos:
                pos[sym].exit_on_next_open = a.reason
                logs.append(f"{t:%H:%M} {sym} {a.reason}")

    for t in clock:
        for sym, tr in traders.items():
            i = pos_i[sym].get(t)
            if i is None:
                continue
            bar = tr.rows[i]
            p = pos.get(sym)
            # 1. exits decided on the previous bar happen at this bar's open
            if p is not None and p.exit_on_next_open:
                close(sym, t, bar.open, "trend break")
                p = None
            # 2. protective stop (checked before new fills: stop first if both)
            if p is not None and p.stop is not None and bar.high >= p.stop:
                close(sym, t, max(p.stop, bar.open), "stop")
                p = None
            # 3. resting sell-stop entries
            for oid, (s, o) in list(orders.items()):
                if s != sym or bar.low > o.price:
                    continue
                px = min(o.price, bar.open)
                orders.pop(oid)
                if sym not in pos:
                    pos[sym] = Position(sym, o.strategy, o.reason, t)
                pos[sym].fills.append((t, o.qty, px, o.tag))
                logs.append(f"{t:%H:%M} {sym} filled {o.tag} {o.qty} @ {px:.2f}")
                for a in tr.on_fill(Fill(oid, sym, "sell", o.qty, px, t), i):
                    handle(sym, t, a)
                own = tr.owner
                if own is not None and getattr(own, "risk_budget", 0):
                    pos[sym].risk_budget = own.risk_budget
                # conservative: if the same bar also reached the stop, assume the stop filled
                p2 = pos.get(sym)
                if p2 is not None and p2.stop is not None and bar.high >= p2.stop and bar.open < p2.stop:
                    close(sym, t, p2.stop, "stop")
            # 4. end of day
            if sym in pos and t.time() >= flatten:
                close(sym, t, bar.close, "end of day")
                continue
            # 5. strategy step on the completed bar
            for a in tr.step(i):
                handle(sym, t, a)
    return pd.DataFrame(trades), logs, traders


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--charts", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    cfg = load_config()
    from data.bars import sessions
    days = [pd.Timestamp(args.day)] if args.day else [
        d for d in sessions("SPY") if pd.Timestamp(args.start) <= d <= pd.Timestamp(args.end)]
    out = ROOT / "reports" / "replay"
    out.mkdir(parents=True, exist_ok=True)
    all_trades = []
    for d in days:
        trades, logs, traders = replay_day(d, cfg)
        (out / f"{d:%Y-%m-%d}.log").write_text("\n".join(logs) + "\n")
        if not args.quiet:
            print("\n".join(logs))
        if len(trades):
            all_trades.append(trades)
            if args.charts:
                from tools.setup_charts import chart_trades
                chart_trades(trades, traders, out)
        print(f"{d:%Y-%m-%d}: {len(trades)} trades")
    if all_trades:
        t = pd.concat(all_trades, ignore_index=True)
        t.to_csv(out / "trades.csv", index=False)
        print(t[["date", "symbol", "strategy", "entry_time", "entry", "adds", "exit_reason", "R", "mfe_R", "mae_R"]]
              .to_string(index=False))


if __name__ == "__main__":
    main()
