"""Event-driven backtest (spec 10).

Each day is replayed minute by minute across the universe: fills against the
bar (simulated broker), then the strategies see the completed bar, then the
Trade Manager adjusts targets and stops. Positions are sized off a fixed
equity, so days are independent and run in parallel; the weekly loss limit is
applied afterwards in date order.

    python -m backtest.runner --start 2018-06-01 --end 2026-10-15 --workers 4 --name base
"""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import time as _time
from datetime import time

import numpy as np
import pandas as pd
import yaml

from data.bars import sessions
from data.fetch import ROOT, load_config
from engine.features import build_features
from engine.rejection import Setup
from engine.trader import DayGate, SymbolTrader
from engine.types import Cancel, ExitAll, Fill, Log, MoveStop, Place
from execution.sim_broker import Pos, SimBroker
from levels.library import build_levels
from risk.limits import RiskBook
from selection.context import has_history
from selection.scanners import spy_pct_change
from selection.watchlist import build_watchlist, universe
from trade.manager import TradeManager


def _bucket(t: pd.Timestamp) -> str:
    hm = t.strftime("%H:%M")
    return ("09:31-10:00" if hm < "10:00" else "10:00-11:30" if hm < "11:30" else
            "11:30-13:30" if hm < "13:30" else "13:30-15:30")


def run_day(day, cfg: dict | None = None, symbols: list[str] | None = None, keep_traders: bool = False):
    """Returns (trades DataFrame, log lines, traders or None)."""
    cfg = cfg or load_config()
    equity = cfg["account"]["equity"]
    day = pd.Timestamp(day).normalize()
    tone, entries = build_watchlist(day, cfg=cfg)
    wl = {e.symbol: e for e in entries}
    gate = DayGate(day, tone, [lv.strength for e in entries for lv in e.resistance(5)], cfg)
    spy = spy_pct_change(day)
    traders: dict[str, SymbolTrader] = {}
    for sym in symbols or universe(cfg):
        if not has_history(sym, day):
            continue
        f = build_features(sym, day, cfg, spy_pct=spy)
        if f.empty or f["atr5"].isna().all():
            continue
        traders[sym] = SymbolTrader(sym, f, build_levels(sym, day), gate, equity, sym in wl, cfg)

    log = [f"{day:%Y-%m-%d} premarket tone {tone}; watchlist: {', '.join(wl)}"]
    broker, risk = SimBroker(cfg), RiskBook(cfg, equity)
    trades: list[dict] = []
    flatten = time.fromisoformat(cfg["session"]["flatten"])
    idx = {s: {t: k for k, t in enumerate(tr.f.index)} for s, tr in traders.items()}
    clock = sorted(set().union(*[tr.f.index for tr in traders.values()])) if traders else []
    last_close: dict[str, float] = {}
    joined: dict[str, str] = {}

    def close_out(sym: str, t: pd.Timestamp, why: str):
        p = broker.pos.pop(sym)
        tr = traders[sym]
        f = tr.f
        e1 = p.fills[0][2]
        risk_usd = sum(q * (p.first_stop - px) for _, q, px, _ in p.fills) if p.first_stop else 0.0
        r_unit = max((p.first_stop or e1) - e1, 1e-9)
        after = f[(f.index >= p.fills[0][0]) & (f.index <= p.fills[0][0] + pd.Timedelta(minutes=60))]
        pnl = p.pnl()
        hits = [w for _, _, _, w in p.covers if w.startswith("T")]
        rec = {
            "date": f"{day:%Y-%m-%d}", "symbol": sym, "strategy": p.strategy, "entry_time": p.fills[0][0],
            "entry": round(e1, 4), "entries": len(p.fills), "adds": sum(1 for *_, tag in p.fills if tag in ("entry2", "add")),
            "shares": p.shares, "avg": round(p.avg, 4), "first_stop": p.first_stop, "exit_time": t,
            "exit": round(p.covers[-1][2], 4), "exit_reason": why, "targets_hit": ",".join(hits),
            "pnl": round(pnl, 2), "risk_usd": round(risk_usd, 2),
            "R": round(pnl / risk_usd, 3) if risk_usd > 0 else np.nan,
            "mfe_R": round((e1 - after["low"].min()) / r_unit, 2), "mae_R": round((after["high"].max() - e1) / r_unit, 2),
            "time_bucket": _bucket(p.fills[0][0]), "premarket_tone": tone.label, **p.meta}
        trades.append(rec)
        log.append(f"{t:%H:%M} {sym} closed ({why}): P&L {pnl:+,.0f} = {rec['R']:+.2f}R")
        risk.day_realized += pnl
        reason = "stop" if why == "stop" and not hits else why
        for a in tr.on_closed(reason):
            act(sym, t, a)

    def act(sym: str, t: pd.Timestamp, a):
        if isinstance(a, Log):
            log.append(f"{t:%H:%M} {a.symbol} {a.text}")
            if "added to the watchlist intraday" in a.text:
                joined[sym] = a.text.split("(")[-1].rstrip(")")
        elif isinstance(a, Place):
            ok, why = risk.allow_entry(sym, sym in broker.pos, broker.busy_symbols())
            if not ok:
                log.append(f"{t:%H:%M} {sym} risk blocked {a.order.tag}: {why}")
                return
            broker.orders[a.order.oid] = a.order
            log.append(f"{t:%H:%M} {sym} sell-stop {a.order.qty} @ {a.order.price:.2f}: {a.order.reason}")
        elif isinstance(a, Cancel):
            broker.orders.pop(a.oid, None)
        elif isinstance(a, MoveStop):
            p = broker.pos.get(sym)
            if p is not None:
                p.stop = a.price if p.stop is None else min(p.stop, a.price)
                p.first_stop = p.first_stop or a.price
                log.append(f"{t:%H:%M} {sym} stop {p.stop:.2f}: {a.reason}")
        elif isinstance(a, ExitAll):
            if sym in broker.pos:
                broker.pos[sym].exit_next_open = "trend break"
                log.append(f"{t:%H:%M} {a.reason}")

    def meta_for(sym: str, t: pd.Timestamp) -> dict:
        tr = traders[sym]
        own = tr.owner
        e = wl.get(sym)
        m = {"live_tone": gate.tone(t).label, "on_watchlist": e is not None,
             "watchlist_reason": ",".join(e.reasons) if e else f"intraday:{joined.get(sym, '')}",
             "focus_stock": bool(e and e.focus), "entry_mode": getattr(own, "entry_mode", "D"),
             "target_mode": cfg["trade"]["target_mode"]}
        if isinstance(own, Setup):
            lv = own.level
            r = own.record
            m.update(level=lv.label() if lv else "", level_kind=lv.kind if lv else "none",
                     level_strength=lv.strength if lv else 0, test=r.get("test"), trigger=r.get("trigger"),
                     extension=r.get("extension", ""), score=r.get("score"), spike=r.get("spike"),
                     size_mult=r.get("size_mult"), **{f"b_{k}": v for k, v in r.get("boosters", {}).items()})
            fo = tr.focus
            m.update(focus_active=fo.active, vdif_focus_start=round(fo.vdif_start, 2),
                     vdif_peak=round(fo.peak_vdif, 2),
                     qs_to_contra_min=fo.qs_to_contra_min if fo.active else None)
        else:
            m.update(level="", level_kind="none", d_move=getattr(own, "move", ""))
        row = tr.rows[idx[sym][t]]
        m.update(contra_tier_at_entry=int(row.contra_short), rvol_at_entry=round(float(row.rvol), 2)
                 if row.rvol == row.rvol else None)
        return m

    for t in clock:
        risk.day_open = sum(p.pnl(last_close.get(s)) - p.pnl() for s, p in broker.pos.items())
        for sym, tr in traders.items():
            i = idx[sym].get(t)
            if i is None:
                continue
            bar = tr.rows[i]
            # 1. exits on this bar (stop first, then at most one target)
            for q, px, why in broker.exits(sym, t, bar):
                p = broker.pos[sym]
                p.covers.append((t, q, px, why))
                log.append(f"{t:%H:%M} {sym} cover {q} @ {px:.2f} ({why})")
                if p.qty <= 0:
                    close_out(sym, t, why)
                elif why.startswith("T"):
                    k = int(why[1]) - 1
                    new = p.tm.on_target(k)
                    if new is not None:
                        new = round(p.avg, 2)   # breakeven on the average entry
                    if new is not None and (p.stop is None or new < p.stop):
                        p.stop = new
                        log.append(f"{t:%H:%M} {sym} stop to breakeven {new:.2f} after {why}")
            # 2. entry fills
            for o, px in broker.entries(sym, bar):
                if sym not in broker.pos:
                    broker.pos[sym] = Pos(sym, o.strategy, t, open_i=i)
                p = broker.pos[sym]
                p.fills.append((t, o.qty, px, o.tag))
                log.append(f"{t:%H:%M} {sym} filled {o.tag} {o.qty} @ {px:.2f}")
                for a in tr.on_fill(Fill(o.oid, sym, "sell", o.qty, px, t), i):
                    act(sym, t, a)
                if len(p.fills) == 1:
                    p.meta = meta_for(sym, t)
                    p.meta["reason"] = o.reason
                    p.tm = TradeManager(sym, o.strategy, cfg, px, p.stop or px * 1.01)
                    for line in p.tm.build(i, tr.f, tr.levels):
                        log.append(f"{t:%H:%M} {sym} {line}")
                # conservative: the same bar also reached the new stop -> stopped out
                if p.stop is not None and bar.high >= p.stop and bar.open < p.stop:
                    p.covers.append((t, p.qty, broker._stop_px(p, bar), "stop"))
                    close_out(sym, t, "stop")
            # 3. end of day
            if sym in broker.pos and t.time() >= flatten:
                p = broker.pos[sym]
                p.covers.append((t, p.qty, float(bar.close), "end of day"))
                close_out(sym, t, "end of day")
            last_close[sym] = float(bar.close)
            if t.time() >= flatten:
                continue
            # 4. strategies on the completed bar
            for a in tr.step(i):
                act(sym, t, a)
            # 5. runner trailing stop
            p = broker.pos.get(sym)
            if p is not None and p.tm is not None:
                new = p.tm.trail(i, tr.f, p.open_i)
                if new is not None and p.stop is not None and new < p.stop:
                    p.stop = new
                    log.append(f"{t:%H:%M} {sym} runner stop trailed to {new:.2f}")
    return pd.DataFrame(trades), log, (traders if keep_traders else None)


# ------------------------------------------------------------------ driver

def _run_chunk(args):
    days, cfg_path = args
    cfg = load_config()
    out, logs = [], []
    for d in days:
        try:
            t, log, _ = run_day(d, cfg)
        except Exception as e:  # keep going; the error is reported
            logs.append(f"{d:%Y-%m-%d} ERROR {type(e).__name__}: {e}")
            continue
        if len(t):
            out.append(t)
        logs.append("\n".join(log))
    return (pd.concat(out, ignore_index=True) if out else pd.DataFrame()), logs


def apply_weekly_limit(trades: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Drop entries made after the week's realized loss reached the limit."""
    if trades.empty:
        return trades
    lim = -cfg["risk"]["weekly_loss_limit"] * cfg["account"]["equity"]
    t = trades.sort_values("entry_time").copy()
    t["week"] = pd.to_datetime(t["date"]).dt.strftime("%G-%V")
    keep = []
    for _, g in t.groupby("week", sort=False):
        for k, r in g.iterrows():
            done = g[(g["exit_time"] <= r["entry_time"]) & g.index.isin(keep)]
            if done["pnl"].sum() > lim:
                keep.append(k)
    return t.loc[keep].drop(columns="week")


def run(start: str, end: str, workers: int = 4, name: str = "base", cfg: dict | None = None) -> pd.DataFrame:
    cfg = cfg or load_config()
    days = [d for d in sessions("SPY") if pd.Timestamp(start) <= d <= pd.Timestamp(end)]
    n = max(1, workers)
    chunks = [days[k::n] for k in range(n)]   # interleaved: every worker sees every era
    t0 = _time.time()
    with mp.get_context("spawn").Pool(n) as pool:
        res = pool.map(_run_chunk, [(c, None) for c in chunks])
    trades = pd.concat([r[0] for r in res if len(r[0])], ignore_index=True) if any(len(r[0]) for r in res) else pd.DataFrame()
    trades = apply_weekly_limit(trades, cfg)
    out = ROOT / "reports" / "backtest" / name
    out.mkdir(parents=True, exist_ok=True)
    split = days[int(len(days) * 0.6)] if days else None
    if len(trades):
        trades["sample"] = np.where(pd.to_datetime(trades["date"]) < split, "in-sample", "out-of-sample")
        trades.sort_values("entry_time").to_csv(out / "trades.csv", index=False)
    (out / "log.txt").write_text("\n\n".join(l for r in res for l in r[1]))
    (out / "config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
    meta = {"start": start, "end": end, "days": len(days), "in_sample_until": str(split.date()) if split else None,
            "trades": len(trades), "seconds": round(_time.time() - t0)}
    (out / "run.json").write_text(json.dumps(meta, indent=2))
    errors = [l for r in res for l in r[1] if " ERROR " in l]
    print(json.dumps(meta), f"errors: {len(errors)}")
    for e in errors[:10]:
        print(e)
    return trades


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2018-06-01")
    ap.add_argument("--end", default="2026-10-15")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--name", default="base")
    a = ap.parse_args()
    t = run(a.start, a.end, a.workers, a.name)
    if len(t):
        g = t.groupby(["sample", "strategy"])
        print(pd.DataFrame({"trades": g.size(), "win%": g["pnl"].apply(lambda x: (x > 0).mean() * 100).round(1),
                            "avg R": g["R"].mean().round(3), "net $": g["pnl"].sum().round(0)}).to_string())


if __name__ == "__main__":
    main()
