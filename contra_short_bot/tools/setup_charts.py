"""1-minute charts of replayed trades: candles, VWAP, the level zone, entries,
stops and the exit, for checking setups against the course's chart examples."""
from __future__ import annotations

import re

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from tools.level_charts import _candles


def chart_trades(trades: pd.DataFrame, traders: dict, out_dir) -> list:
    paths = []
    for r in trades.itertuples():
        tr = traders[r.symbol]
        f = tr.f
        t0 = r.entry_time - pd.Timedelta(minutes=45)
        t1 = min(r.exit_time + pd.Timedelta(minutes=20), f.index[-1])
        v = f[(f.index >= t0) & (f.index <= t1)]
        fig, ax = plt.subplots(figsize=(13, 6.5))
        _candles(ax, v, 0)
        x = {t: k for k, t in enumerate(v.index)}
        ax.plot(range(len(v)), v["vwap"], color="#7048e8", linewidth=1, label="VWAP")
        m = re.search(r"of (.+?) (\d+\.\d+)-(\d+\.\d+)", r.reason)
        if m:
            lo, hi = float(m.group(2)), float(m.group(3))
            ax.axhspan(lo, hi, color="#2a6fdb", alpha=0.15, linewidth=0)
            ax.text(1, hi, f" {m.group(1)} zone", color="#2a6fdb", fontsize=8, va="bottom")
        if r.entry_time in x:
            ax.plot(x[r.entry_time], r.entry, "v", color="#c92a2a", markersize=9)
        if r.first_stop == r.first_stop and r.first_stop:
            ax.axhline(r.first_stop, color="#c92a2a", linestyle="--", linewidth=0.8)
            ax.text(len(v) - 1, r.first_stop, " first stop", color="#c92a2a", fontsize=8, va="bottom", ha="right")
        if r.exit_time in x:
            ax.plot(x[r.exit_time], r.exit, "^", color="#2f9e44", markersize=9)
        step = max(1, len(v) // 12)
        ax.set_xticks(range(0, len(v), step))
        ax.set_xticklabels([t.strftime("%H:%M") for t in v.index[::step]], fontsize=8)
        ax.set_title(f"{r.symbol} {r.date}  {r.strategy}  entry {r.entry:.2f} @ {r.entry_time:%H:%M}  "
                     f"exit {r.exit:.2f} ({r.exit_reason})  R {r.R:+.2f}  MFE {r.mfe_R:.1f}R  MAE {r.mae_R:.1f}R",
                     fontsize=10)
        ax.text(0.01, 0.01, r.reason[:160], transform=ax.transAxes, fontsize=7.5, color="#495057")
        ax.grid(alpha=0.2)
        fig.tight_layout()
        p = out_dir / f"{r.date}_{r.symbol}_{r.strategy}_{r.entry_time:%H%M}.png"
        fig.savefig(p, dpi=105)
        plt.close(fig)
        paths.append(p)
    return paths
