"""Backtest: weekly ~10-delta, ~90 DTE short put on SPY.

Rules
- Entry: first trading day of each week (Monday, or the next session if
  Monday is a holiday), at the close.
- Expiry: the listed expiration closest to 90 calendar DTE.
- Strike: the put whose delta is closest to -0.10 (delta computed from the
  quote mid, see pricing.py).
- 1 contract per entry; positions overlap.
- Exit, whichever first: put price <= 50% of credit (checked at each close,
  filled at that close's price), or the first close at <= 21 DTE.
  No stop loss.

Fill modes
- mid:          enter at mid, exit at mid, mark open positions at mid.
- conservative: enter at bid, exit at ask, mark open positions at ask.
The same contract is chosen in both modes; only the fills differ.

Input: one parquet per trading day in --data-dir (see databento_loader.py)
with columns expiration, right, strike, bid, ask.
"""
import argparse
import json
import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import exchange_calendars as xcals
import numpy as np
import pandas as pd

from data_sources import DatabentoSource, LocalChainSource, forward_for_expiry, load_env
from pricing import implied_vol_put, put_delta, regt_naked_put_bp

HERE = Path(__file__).parent


@dataclass
class Params:
    target_dte: int = 90
    target_delta: float = -0.10
    profit_target: float = 0.50   # close when price <= this fraction of credit
    exit_dte: int = 21
    commission: float = 1.00      # $ per contract per side
    fill_mode: str = "mid"        # "mid" or "conservative"


@dataclass
class Position:
    entry_date: date
    expiration: date
    strike: float
    credit: float
    entry_delta: float
    entry_iv: float
    entry_spot: float
    mark: float = float("nan")
    last_quote_date: date = None
    exit_date: date = None
    exit_price: float = None
    exit_reason: str = None
    days_held: int = 0
    notes: list = field(default_factory=list)


# ---------------------------------------------------------------- selection

def mid_of(bid, ask):
    if not np.isfinite(ask) or ask <= 0:
        return float("nan")
    return 0.5 * (max(bid, 0.0) + ask)


def select_entry(chain, d, spot, p: Params):
    """Pick expiry closest to target DTE, then the put with delta closest to target."""
    expiries = sorted(e for e in chain["expiration"].unique() if (e - d).days > p.exit_dte)
    if not expiries:
        return None, "no expiries"
    exp = min(expiries, key=lambda e: (abs((e - d).days - p.target_dte), e))
    dte = (exp - d).days
    T = dte / 365.0
    q = chain[(chain["bid"] > 0) & np.isfinite(chain["ask"])]
    F, DF = forward_for_expiry(q, exp, spot)
    note = ""
    if F is None:
        F, DF, note = spot, 1.0, "parity fit failed; used spot as forward, DF=1"
    puts = q[(q["expiration"] == exp) & (q["right"] == "P")].copy()
    if puts.empty:
        return None, f"no quoted puts for {exp}"
    puts["mid"] = (puts["bid"] + puts["ask"]) / 2
    puts["iv"] = implied_vol_put(puts["mid"].values, F, puts["strike"].values, T, DF)
    spot_factor = float(np.clip(DF * F / spot, 0.9, 1.0)) if spot else 1.0
    puts["delta"] = put_delta(F, puts["strike"].values, T, puts["iv"].values, spot_factor)
    puts = puts[np.isfinite(puts["delta"])]
    if puts.empty:
        return None, f"no valid IVs for {exp}"
    row = puts.loc[(puts["delta"] - p.target_delta).abs().idxmin()]
    return dict(expiration=exp, dte=dte, strike=float(row["strike"]), bid=float(row["bid"]),
                ask=float(row["ask"]), mid=float(row["mid"]), iv=float(row["iv"]),
                delta=float(row["delta"]), forward=F, df=DF, note=note), None


# ---------------------------------------------------------------- engine

def entry_days(sessions):
    """First session of each Mon-Sun week = Monday, or next session if Monday is closed."""
    s = pd.Series(sessions, index=pd.DatetimeIndex(sessions))
    week = s.index.to_period("W-SUN")
    return set(s.groupby(week).first().tolist())


def sessions_between(start, end):
    cal = xcals.get_calendar("XNYS")
    ss = cal.sessions_in_range(pd.Timestamp(start), pd.Timestamp(end))
    return [x.date() for x in ss], {x.date(): c for x, c in zip(ss, cal.closes[ss])}


def run(source, start, end, p: Params, log=None):
    sessions, _ = sessions_between(start, end)
    entries = entry_days(sessions)
    open_pos, closed, skipped, daily = [], [], [], []
    realized = 0.0
    prev_spot = None

    def exit_side(bid, ask):
        return mid_of(bid, ask) if p.fill_mode == "mid" else (ask if np.isfinite(ask) else float("nan"))

    for n, d in enumerate(sessions):
        if log and n % 50 == 0:
            log(f"  {d}  open {len(open_pos):2d}  closed {len(closed)}")
        chain = source.entry_chain(d) if d in entries else None
        quotes = source.put_quotes(d, [(pos.expiration, pos.strike) for pos in open_pos])
        spot = source.spot(d) or prev_spot
        prev_spot = spot

        # 1) manage open positions (no quote today -> keep last mark, check again tomorrow)
        still_open = []
        for pos in open_pos:
            pos.days_held += 1
            bid, ask = quotes.get((pos.expiration, pos.strike), (np.nan, np.nan))
            px = exit_side(bid, ask)
            if np.isfinite(px):
                pos.mark, pos.last_quote_date = px, d
            dte = (pos.expiration - d).days
            reason = None
            if np.isfinite(px) and px <= p.profit_target * pos.credit:
                reason = "profit_target"
            elif dte <= p.exit_dte:
                if np.isfinite(px):
                    reason = "time_21dte"
                else:
                    pos.notes.append(f"{d}: no quote at exit DTE, retry next day")
            if reason:
                pos.exit_date, pos.exit_price, pos.exit_reason = d, px, reason
                realized += (pos.credit - px) * 100 - p.commission
                closed.append(pos)
            else:
                still_open.append(pos)
        open_pos = still_open

        # 2) new entry
        if d in entries:
            if chain is None or chain.empty:
                skipped.append(dict(date=d, reason="no chain data"))
            elif spot is None:
                skipped.append(dict(date=d, reason="could not estimate spot"))
            else:
                sel, err = select_entry(chain, d, spot, p)
                if sel is None:
                    skipped.append(dict(date=d, reason=err))
                else:
                    credit = sel["mid"] if p.fill_mode == "mid" else sel["bid"]
                    pos = Position(entry_date=d, expiration=sel["expiration"], strike=sel["strike"],
                                   credit=credit, entry_delta=sel["delta"], entry_iv=sel["iv"],
                                   entry_spot=spot, mark=exit_side(sel["bid"], sel["ask"]),
                                   last_quote_date=d)
                    if sel["note"]:
                        pos.notes.append(sel["note"])
                    open_pos.append(pos)
                    realized -= p.commission  # entry commission paid now

        # 3) mark to market and Reg T buying power (premium at mid)
        unreal = sum((pos.credit - pos.mark) * 100 for pos in open_pos if np.isfinite(pos.mark))
        bp = 0.0
        for pos in open_pos:
            bid, ask = quotes.get((pos.expiration, pos.strike), (np.nan, np.nan))
            prem = mid_of(bid, ask)
            prem = prem if np.isfinite(prem) else pos.mark
            bp += regt_naked_put_bp(spot, pos.strike, prem) if spot else 0.0
        stale = sum(1 for pos in open_pos if pos.last_quote_date != d)
        daily.append(dict(date=d, spot=round(spot, 2) if spot else None, open_positions=len(open_pos),
                          stale_marks=stale, realized=round(realized, 2), unrealized=round(unreal, 2),
                          equity=round(realized + unreal, 2), buying_power=round(bp, 2)))

    trades = _trade_frame(closed, p, open_pos)
    return trades, pd.DataFrame(daily), pd.DataFrame(skipped, columns=["date", "reason"])


def _trade_frame(closed, p, open_pos):
    rows = []
    for pos in closed + open_pos:
        is_open = pos.exit_date is None
        rows.append(dict(
            entry_date=pos.entry_date, strike=pos.strike, expiry=pos.expiration,
            entry_dte=(pos.expiration - pos.entry_date).days,
            entry_delta=round(pos.entry_delta, 4), entry_iv=round(pos.entry_iv, 4),
            entry_spot=round(pos.entry_spot, 2) if pos.entry_spot else None,
            credit=round(pos.credit, 4),
            exit_date=None if is_open else pos.exit_date,
            exit_reason="open" if is_open else pos.exit_reason,
            exit_price=round(pos.mark if is_open else pos.exit_price, 4),
            pnl=None if is_open else round((pos.credit - pos.exit_price) * 100 - 2 * p.commission, 2),
            notes="; ".join(pos.notes),
        ))
    return pd.DataFrame(rows).sort_values("entry_date").reset_index(drop=True) if rows else pd.DataFrame()


# ---------------------------------------------------------------- reporting

def summarize(trades):
    t = trades[trades["exit_reason"] != "open"] if len(trades) else trades
    if t.empty:
        return dict(trades=0)
    pnl = t["pnl"]
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    gross_loss = -losses.sum()
    return dict(
        trades=int(len(t)),
        win_rate=round(len(wins) / len(t), 4),
        avg_win=round(wins.mean(), 2) if len(wins) else 0.0,
        avg_loss=round(losses.mean(), 2) if len(losses) else 0.0,
        largest_loss=round(pnl.min(), 2),
        total_pnl=round(pnl.sum(), 2),
        expectancy=round(pnl.mean(), 2),
        profit_factor=round(wins.sum() / gross_loss, 2) if gross_loss > 0 else float("inf"),
        profit_target_exits=int((t["exit_reason"] == "profit_target").sum()),
        time_exits=int(t["exit_reason"].str.startswith("time").sum()),
        avg_days_held=round((pd.to_datetime(t["exit_date"]) - pd.to_datetime(t["entry_date"])).dt.days.mean(), 1),
    )


def drawdown_stats(eq):
    if eq.empty:
        return {}
    e = eq.set_index(pd.to_datetime(eq["date"]))["equity"]
    dd = e - e.cummax()
    trough = dd.idxmin()
    peak_bp = eq["buying_power"].max()
    return dict(
        max_drawdown=round(dd.min(), 2),
        max_drawdown_trough=str(trough.date()),
        max_drawdown_peak=str(e[:trough].idxmax().date()),
        peak_buying_power=round(peak_bp, 2),
        peak_buying_power_date=str(pd.to_datetime(eq.loc[eq["buying_power"].idxmax(), "date"]).date()),
        avg_buying_power=round(eq.loc[eq["open_positions"] > 0, "buying_power"].mean(), 2),
        max_drawdown_pct_of_peak_bp=round(100 * dd.min() / peak_bp, 2) if peak_bp else None,
        max_concurrent_positions=int(eq["open_positions"].max()),
    )


def by_year(trades, eq):
    t = trades[trades["exit_reason"] != "open"].copy()
    t["year"] = pd.to_datetime(t["exit_date"]).dt.year
    rows = []
    e = eq.set_index(pd.to_datetime(eq["date"]))["equity"]
    for y, g in t.groupby("year"):
        s = summarize(g)
        ey = e[e.index.year == y]
        prior = e[e.index.year < y]
        base = prior.iloc[-1] if len(prior) else 0.0
        dd = (ey - pd.concat([pd.Series([base]), ey]).cummax().iloc[1:].values).min() if len(ey) else 0.0
        rows.append(dict(year=y, trades=s["trades"], win_rate=s["win_rate"], total_pnl=s["total_pnl"],
                         expectancy=s["expectancy"], largest_loss=s["largest_loss"],
                         mtm_equity_change=round(ey.iloc[-1] - base, 2) if len(ey) else 0.0,
                         max_drawdown_in_year=round(dd, 2)))
    return pd.DataFrame(rows)


def charts(trades, eq, out, title):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    dates = pd.to_datetime(eq["date"])
    dd = eq["equity"] - eq["equity"].cummax()
    ink, muted, accent, loss = "#1f2933", "#7b8794", "#2563eb", "#dc2626"

    fig, ax = plt.subplots(figsize=(11, 4.5))
    ax.plot(dates, eq["equity"], color=accent, lw=1.4, label="Equity (mark-to-market)")
    ax.plot(dates, eq["realized"], color=muted, lw=1, ls="--", label="Realized only")
    ax.axhline(0, color=muted, lw=0.6)
    ax.set_title(f"Equity curve, {title}", color=ink, loc="left")
    ax.set_ylabel("Cumulative P&L ($, 1 contract/week)")
    ax.legend(frameon=False)
    ax.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(out / "equity_curve.png", dpi=130)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(11, 3.5))
    ax.fill_between(dates, dd, 0, color=loss, alpha=0.35, lw=0)
    ax.plot(dates, dd, color=loss, lw=0.8)
    ax.set_title(f"Drawdown from equity peak, {title}", color=ink, loc="left")
    ax.set_ylabel("$")
    ax.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(out / "drawdown.png", dpi=130)
    plt.close(fig)

    t = trades[trades["exit_reason"] != "open"]
    if not t.empty:
        fig, ax = plt.subplots(figsize=(9, 4))
        bins = np.histogram_bin_edges(t["pnl"], bins=60)
        ax.hist(t.loc[t["pnl"] > 0, "pnl"], bins=bins, color=accent, alpha=0.85, label="Wins")
        ax.hist(t.loc[t["pnl"] <= 0, "pnl"], bins=bins, color=loss, alpha=0.85, label="Losses")
        ax.set_yscale("log")
        ax.set_title(f"Per-trade P&L, {title} (log count)", color=ink, loc="left")
        ax.set_xlabel("P&L per trade ($)")
        ax.legend(frameon=False)
        ax.grid(alpha=0.25)
        fig.tight_layout()
        fig.savefig(out / "pnl_histogram.png", dpi=130)
        plt.close(fig)


def report(trades, eq, skipped, out, title, recent_years=5):
    out.mkdir(parents=True, exist_ok=True)
    trades.to_csv(out / "trades.csv", index=False)
    eq.to_csv(out / "equity_daily.csv", index=False)
    skipped.to_csv(out / "skipped_entries.csv", index=False)
    yearly = by_year(trades, eq) if len(trades) else pd.DataFrame()
    yearly.to_csv(out / "by_year.csv", index=False)

    last = pd.to_datetime(eq["date"]).max()
    cutoff = (last - pd.DateOffset(years=recent_years)).date()
    recent = trades[pd.to_datetime(trades["entry_date"]).dt.date >= cutoff]
    eq_recent = eq[pd.to_datetime(eq["date"]).dt.date >= cutoff].copy()
    if not eq_recent.empty:
        base = eq_recent["equity"].iloc[0]
        eq_recent["equity"] -= base
    summary = dict(
        title=title,
        period=f"{eq['date'].iloc[0]} .. {eq['date'].iloc[-1]}",
        full=summarize(trades) | drawdown_stats(eq),
        recent=dict(since=str(cutoff), **summarize(recent), **drawdown_stats(eq_recent)),
        open_positions_at_end=int((trades["exit_reason"] == "open").sum()) if len(trades) else 0,
        skipped_entries=int(len(skipped)),
    )
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    charts(trades, eq, out, title)
    return summary


SOURCE_CLAIM = dict(trades=220, win_rate=0.977, expectancy=141.0)


def _usd(v):
    return f"-${-v:,.0f}" if v < 0 else f"${v:,.0f}"


def comparison_text(results):
    """Side-by-side table: full period and last 5 years for each fill mode, vs the source claim."""
    rows = [("", "trades", "win rate", "avg win", "avg loss", "worst", "total P&L", "per trade", "PF",
             "max DD", "DD % peak BP", "peak BP")]
    for mode, r in results.items():
        for part in ("full", "recent"):
            x = r[part]
            if not x.get("trades"):
                continue
            label = f"{mode} {'all' if part == 'full' else 'since ' + x['since']}"
            rows.append((label, x["trades"], f"{x['win_rate']:.1%}", _usd(x['avg_win']), _usd(x['avg_loss']),
                         _usd(x['largest_loss']), _usd(x['total_pnl']), _usd(x['expectancy']),
                         f"{x['profit_factor']:.2f}", _usd(x.get('max_drawdown', 0)),
                         f"{x.get('max_drawdown_pct_of_peak_bp') or 0:.0f}%", _usd(x.get('peak_buying_power', 0))))
    c = SOURCE_CLAIM
    rows.append(("source claim (~5 yrs)", c["trades"], f"{c['win_rate']:.1%}", "", "", "", "",
                 _usd(c['expectancy']), "", "", "", ""))
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    lines = ["  ".join(str(v).rjust(w) if i else str(v).ljust(w) for i, (v, w) in enumerate(zip(r, widths)))
             for r in rows]
    period = next(iter(results.values()))["period"]
    return f"Weekly 10-delta ~90 DTE SPY short put, {period}\n\n" + "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="2013-04-01", help="cbbo-1m history starts 2013-04-01")
    ap.add_argument("--end", default=None, help="default: 2 trading days ago (Databento) / last file (--data-dir)")
    ap.add_argument("--out", default=str(HERE / "results"))
    ap.add_argument("--commission", type=float, default=1.00, help="$ per contract per side")
    ap.add_argument("--modes", default="mid,conservative")
    ap.add_argument("--data-dir", default=None, help="use local full-chain parquet files instead of Databento")
    ap.add_argument("--offline", action="store_true", help="Databento cache only, no downloads")
    ap.add_argument("--yes", action="store_true", help="skip the cost prompt")
    args = ap.parse_args()

    if args.data_dir:
        source = LocalChainSource(args.data_dir)
        end = args.end or str(source.last_day())
        args.start = max(args.start, str(source.first_day()))
    else:
        found = load_env()
        if not args.offline and not os.environ.get("DATABENTO_API_KEY"):
            where = "\n  ".join(str(f) for f in found) or "(no .env found)"
            raise SystemExit(f"Missing DATABENTO_API_KEY. Looked in:\n  {where}\n"
                             f"Put DATABENTO_API_KEY=... in {HERE / '.env'}")
        source = DatabentoSource(offline=args.offline)
        end = args.end or str((pd.Timestamp.today() - pd.offsets.BDay(2)).date())
        sessions, closes = sessions_between(args.start, end)
        source.set_sessions(closes)
        if not args.offline:
            ents = sorted(entry_days(sessions))
            total, detail = source.estimate_cost(ents, sessions)
            print(f"Databento estimate: ~${total:,.2f}  ({detail})")
            if total > 0 and not args.yes and input("Continue? [y/N] ").strip().lower() != "y":
                raise SystemExit("Stopped before downloading.")

    results = {}
    for mode in args.modes.split(","):
        print(f"Running {mode} fills {args.start} .. {end}")
        p = Params(commission=args.commission, fill_mode=mode)
        trades, eq, skipped = run(source, args.start, end, p, log=print)
        label = "mid fills" if mode == "mid" else "bid entry / ask exit"
        results[mode] = report(trades, eq, skipped, Path(args.out) / mode, label)
        print(json.dumps(results[mode], indent=2, default=str))
    (Path(args.out) / "summary_all.json").write_text(json.dumps(results, indent=2, default=str))
    text = comparison_text(results)
    (Path(args.out) / "report.txt").write_text(text)
    print(text)
    if not args.data_dir:
        print(f"Databento requests this run: {source.requests}")


if __name__ == "__main__":
    main()
