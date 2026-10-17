"""Daily chart images of the levels found for random (symbol, day) pairs, for a
visual check against TradingView before trusting any backtest (spec 11).

    python -m tools.level_charts --n 20
    python -m tools.level_charts --symbol NVDA --day 2025-04-21

Each chart shows the ~250 sessions before the day with every merged level zone
within 6 x ATRd of the prior close drawn across it and trendlines from their first anchor. The 20 sessions after
the day are drawn faded, so you can see whether price reacted at the levels.
"""
from __future__ import annotations

import argparse
import random

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import yaml

from data.bars import adjust_daily, daily_bars
from data.fetch import ROOT, load_config
from levels.library import build_levels_from_daily

COLORS = {"pivot": "#2a6fdb", "manual": "#7b3fbf", "trendline": "#d9480f", "gap_fill": "#c92a2a",
          "gap_entry": "#e8590c", "sma": "#2b8a3e", "prior_high": "#868e96", "psych": "#adb5bd"}


def _candles(ax, df: pd.DataFrame, x0: int, alpha: float = 1.0) -> None:
    x = np.arange(x0, x0 + len(df))
    up = df["close"] >= df["open"]
    col = np.where(up, "#2f9e44", "#e03131")
    ax.vlines(x, df["low"], df["high"], color=col, linewidth=0.7, alpha=alpha)
    body_lo = np.minimum(df["open"], df["close"])
    body_h = np.maximum((df["close"] - df["open"]).abs(), 1e-6)
    ax.bar(x, body_h, bottom=body_lo, width=0.7, color=col, alpha=alpha, linewidth=0)


def chart(symbol: str, day: pd.Timestamp, out_dir, cfg: dict, before: int = 250, after: int = 20,
          span_atr: float = 6.0):
    daily = adjust_daily(daily_bars(symbol), symbol, day)
    dl = build_levels_from_daily(daily, symbol, day, cfg)
    hist = daily[daily.index < day].iloc[-before:]
    fut = daily[daily.index >= day].iloc[:after]
    fig, ax = plt.subplots(figsize=(14, 7))
    _candles(ax, hist, 0)
    _candles(ax, fut, len(hist), alpha=0.35)
    ax.axvline(len(hist) - 0.5, color="black", linewidth=0.8, linestyle=":")
    view = pd.concat([hist, fut])
    lo, hi = view["low"].min(), view["high"].max()
    pad = (hi - lo) * 0.05
    ax.set_ylim(lo - pad, hi + pad)
    xr = len(hist) + len(fut)
    pos = {d: i for i, d in enumerate(daily.index)}
    first = pos[hist.index[0]]
    for lv in dl.levels:
        if lv.zone_high < lo - pad or lv.zone_low > hi + pad:
            continue
        if abs(lv.mid - dl.prior_close) > span_atr * dl.atr_d:
            continue  # the bot only acts near price; far-away levels just clutter the chart
        c = COLORS.get(lv.kind, "#495057")
        ax.axhspan(lv.zone_low, lv.zone_high, color=c, alpha=0.18, linewidth=0)
        side = "R" if lv.zone_high > dl.prior_close else "S"
        ax.text(xr + 0.5, lv.mid, f"{side} {lv.label()}  s={lv.strength:g}", color=c, fontsize=7.5, va="center")
    for lv in dl.raw:
        if lv.kind == "trendline" and lv.line:
            d0, p0, d1, p1, _, _ = lv.line
            i0, i1 = pos[d0], pos[d1]  # positions in the full daily index
            xs = np.array([max(i0, first), pos[hist.index[-1]] + 1 + len(fut)])
            ys = p0 + (p1 - p0) * (xs - i0) / (i1 - i0)
            ax.plot(xs - first, ys, color=COLORS["trendline"], linewidth=1, alpha=0.8)
            if "channel" in lv.notes:
                continue  # a rail is shifted up from its anchors, so no anchor dots
            for d, p in ((d0, p0), (d1, p1)):
                if d in pos and pos[d] >= first:
                    ax.plot(pos[d] - first, p, "o", color=COLORS["trendline"], markersize=3)
    step = max(1, len(view) // 12)
    ax.set_xticks(range(0, len(view), step))
    ax.set_xticklabels([d.strftime("%Y-%m-%d") for d in view.index[::step]], rotation=30, fontsize=7)
    ax.set_xlim(-1, xr + 28)
    ax.set_title(f"{symbol}  levels before the open on {day:%Y-%m-%d}   prior close {dl.prior_close:.2f}   "
                 f"ATRd {dl.atr_d:.2f}   (faded = the 20 sessions after)", fontsize=10)
    ax.grid(alpha=0.2)
    fig.tight_layout()
    path = out_dir / f"{symbol}_{day:%Y-%m-%d}.png"
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return path, dl


def prep_rows(dl) -> dict:
    """One morning-prep-sheet row (spec 6.5) from a DayLevels."""
    res = dl.resistance()[:2]
    sup = dl.support()[:3]
    return {"Stock": dl.symbol, "Key Support": ", ".join(l.label() for l in sup),
            "Key Resistance": ", ".join(l.label() for l in res)}


def main() -> None:
    cfg = load_config()
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--symbol")
    ap.add_argument("--day")
    ap.add_argument("--start", default="2023-06-01")
    ap.add_argument("--end", default="2026-09-15")
    ap.add_argument("--out", default=str(ROOT / "reports" / "levels"))
    args = ap.parse_args()
    out = __import__("pathlib").Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if args.symbol:
        pairs = [(args.symbol, pd.Timestamp(args.day))]
    else:
        favs = yaml.safe_load((ROOT / "watchlist.yaml").read_text())["favorites"]
        rnd = random.Random(args.seed)
        days = pd.bdate_range(args.start, args.end)
        pairs = []
        for sym in rnd.sample(favs, min(args.n, len(favs))) + [rnd.choice(favs) for _ in range(max(0, args.n - len(favs)))]:
            d = daily_bars(sym).index
            d = d[(d >= days[0]) & (d <= days[-1])]
            pairs.append((sym, rnd.choice(list(d))))
    rows = []
    for sym, day in pairs:
        path, dl = chart(sym, day, out, cfg)
        rows.append({"Day": f"{day:%Y-%m-%d}", **prep_rows(dl)})
        print(path)
    md = ["| Day | Stock | Key Support | Key Resistance |", "|---|---|---|---|"]
    md += [f"| {r['Day']} | {r['Stock']} | {r['Key Support']} | {r['Key Resistance']} |" for r in rows]
    (out / "levels_table.md").write_text("\n".join(md) + "\n")
    print(out / "levels_table.md")


if __name__ == "__main__":
    main()
