"""Write run outputs: trade log, daily series, summary, equity chart."""
from __future__ import annotations

import json
import os

import pandas as pd

from .engine import Result
from .metrics import summarize

# Reference palette (dataviz skill): categorical slots 1-2, light surface, text inks.
SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
BLUE, ORANGE = "#2a78d6", "#eb6834"


def trade_log(res: Result) -> pd.DataFrame:
    rows = res.trades.assign(skip_reason="")
    if len(res.skips):
        rows = pd.concat([rows, res.skips], ignore_index=True, sort=False)
    return rows.sort_values("entry_date", kind="stable").reset_index(drop=True)


def write_report(res: Result, out_dir: str, chart: bool = True) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    summary = summarize(res)
    trade_log(res).to_csv(os.path.join(out_dir, "trades.csv"), index=False)
    res.daily.to_csv(os.path.join(out_dir, "daily.csv"), index=False)
    with open(os.path.join(out_dir, "summary.json"), "w") as fh:
        json.dump(summary, fh, indent=2, default=str)
    with open(os.path.join(out_dir, "summary.md"), "w") as fh:
        fh.write(summary_markdown(summary))
    if chart and len(res.daily):
        equity_chart(res, os.path.join(out_dir, "equity.png"), summary)
    return summary


def _money(x):
    return "n/a" if x is None else f"${x:,.0f}"


def _pct(x):
    return "n/a" if x is None else f"{x:.1f}%"


def summary_markdown(s: dict) -> str:
    wr = "n/a" if s["win_rate"] is None else f"{100 * s['win_rate']:.1f}%"
    lines = [
        f"# Broken-wing put condor: {s['product']} (${s['usd_per_spx_point']:g} per SPX point)",
        "",
        f"Window: {s['window']}  ",
        f"Option prices: **{s['chain_source']}**",
        "",
    ]
    for w in s["warnings"]:
        lines.append(f"> **Warning:** {w}")
    lines += [
        "",
        "| Metric | Value |", "|---|---|",
        f"| Trades (closed / open at end) | {s['trades']} ({s['closed_trades']} / {s['open_at_end']}) |",
        f"| Skipped weeks | {s['skipped']} {s['skip_reasons'] or ''} |",
        f"| Win rate | {wr} ({s['wins']} W / {s['losses']} L) |",
        f"| Avg win / avg loss | {_money(s['avg_win_usd'])} / {_money(s['avg_loss_usd'])} |",
        f"| Worst trade | {_money(s['worst_trade_usd'])} ({s['full_max_loss_trades']} full max-loss) |",
        f"| Avg credit | {s['avg_credit_pts']} SPX pts = {_money(s['avg_credit_usd'])} |",
        f"| Avg short 10-delta strike (L3) below spot | {_pct(s['avg_l3_pct_below_spot'])} |",
        f"| Total P&L (closed / incl. open MTM) | {_money(s['total_pnl_closed_usd'])} / {_money(s['total_pnl_mtm_usd'])} |",
        f"| Fees | {_money(s['fees_usd'])} ({_pct(s['fees_pct_of_gross_credit'])} of gross credit) |",
        f"| Max drawdown, mark-to-market | {_money(s['max_drawdown_mtm_usd'])} on {s['max_drawdown_mtm_date']} |",
        f"| Max drawdown, realized | {_money(s['max_drawdown_realized_usd'])} |",
        f"| Max concurrent positions | {s['max_concurrent_positions']} |",
        f"| Worst-case aggregate loss (all open at max loss) | {_money(s['worst_case_aggregate_loss_usd'])} |",
        f"| Peak modeled buying power | {_money(s['peak_buying_power_usd'])} |",
        f"| ROC on Reg-T capital (peak aggregate max loss) | {_pct(s['roc_regt_pct'])} (CAGR {_pct(s['cagr_on_regt_capital_pct'])}) |",
        f"| ROC on peak modeled buying power | {_pct(s['roc_peak_bp_pct'])} (CAGR {_pct(s['cagr_on_peak_bp_pct'])}) |",
        f"| Avg trade return on its max loss | {_pct(s['avg_trade_roc_on_max_loss_pct'])} |",
        f"| Avg position delta at entry | {s['avg_position_delta_at_entry']} |",
        f"| SPX buy & hold, same window | {_pct(s['spx_buy_hold_pct'])} |",
        "",
        "Outcomes: " + ", ".join(f"{k} {v}" for k, v in s["outcomes"].items()),
        "",
    ]
    if s["losses_by_expiration_month"]:
        lines += ["## Losses by expiration month", "", "| Month | Losing trades | P&L |", "|---|---|---|"]
        lines += [f"| {m} | {v['count']} | {_money(v['pnl_usd'])} |" for m, v in s["losses_by_expiration_month"].items()]
    return "\n".join(lines) + "\n"


def equity_chart(res: Result, path: str, s: dict):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    d = res.daily.copy()
    d["date"] = pd.to_datetime(d["date"])
    plt.rcParams.update({"font.size": 9, "axes.edgecolor": GRID, "axes.labelcolor": INK2,
                         "xtick.color": INK2, "ytick.color": INK2, "text.color": INK})
    fig, axes = plt.subplots(3, 1, figsize=(11, 8.5), sharex=True, facecolor=SURFACE,
                             gridspec_kw={"height_ratios": [3, 1.4, 1.6], "hspace": 0.12})
    for ax in axes:
        ax.set_facecolor(SURFACE)
        ax.grid(axis="y", color=GRID, linewidth=0.8)
        ax.spines[["top", "right", "left"]].set_visible(False)
        ax.tick_params(length=0)
        ax.yaxis.set_major_formatter(matplotlib.ticker.StrMethodFormatter("${x:,.0f}"))

    ax = axes[0]
    ax.plot(d["date"], d["realized_pnl"], color=BLUE, linewidth=2, label="Realized")
    ax.plot(d["date"], d["mtm_pnl"], color=ORANGE, linewidth=2, label="Mark-to-market")
    ax.axhline(0, color=INK2, linewidth=0.8)
    ax.set_title(f"{s['product']} broken-wing put condor: cumulative P&L "
                 f"({s['chain_source']} prices, ${s['usd_per_spx_point']:g}/SPX pt)",
                 loc="left", fontsize=11, color=INK)
    ax.legend(loc="lower right", bbox_to_anchor=(1, 1.0), ncol=2, frameon=False, borderaxespad=0.2)

    ax = axes[1]
    dd = d["mtm_pnl"] - d["mtm_pnl"].cummax()
    ax.fill_between(d["date"], dd, 0, color=ORANGE, alpha=0.35, linewidth=0)
    ax.plot(d["date"], dd, color=ORANGE, linewidth=1)
    ax.set_title("Mark-to-market drawdown", loc="left", fontsize=10, color=INK2)

    ax = axes[2]
    ax.plot(d["date"], d["aggregate_max_loss"], color=BLUE, linewidth=2, label="Sum of open max losses")
    ax.plot(d["date"], d["buying_power"], color=ORANGE, linewidth=2, label="Modeled buying power")
    ax.set_title("Capital at risk", loc="left", fontsize=10, color=INK2)
    ax.legend(loc="lower right", bbox_to_anchor=(1, 1.0), ncol=2, frameon=False, borderaxespad=0.2)

    fig.savefig(path, dpi=130, bbox_inches="tight", facecolor=SURFACE)
    plt.close(fig)
