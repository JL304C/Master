"""Parameter sensitivity and mode comparisons (spec 10-11), in-sample only.

Each variant changes one setting from the base config and is run on every n-th
in-sample session, so comparisons are quick and never touch the out-of-sample
period. Results go to reports/backtest/sweep/summary.csv and are shown in the
report.

    python -m backtest.sweep --every 4
"""
from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd

from backtest.runner import run
from data.bars import sessions
from data.fetch import ROOT

# (group, label, overrides). The base run is included once as the reference.
VARIANTS = [
    ("base", "base config", {}),
    # The 5 defaults the spec asks to test (section 10).
    ("extension threshold", "speed 1.5 x ATR5", {"engine": {"extension": {"speed_atr5": 1.5}}}),
    ("extension threshold", "speed 2.5 x ATR5", {"engine": {"extension": {"speed_atr5": 2.5}}}),
    ("minutes outside zone", "2 bars", {"engine": {"max_bars_outside": 2}}),
    ("minutes outside zone", "5 bars", {"engine": {"max_bars_outside": 5}}),
    ("pivot touch minimum", "2 touches", {"levels": {"pivot": {"min_touches": 2}}}),
    ("pivot touch minimum", "4 touches", {"levels": {"pivot": {"min_touches": 4}}}),
    ("Scenario B buffer", "0.25 x ATR1", {"engine": {"scenario_b": {"atr1": 0.25}}}),
    ("Scenario B buffer", "1.0 x ATR1", {"engine": {"scenario_b": {"atr1": 1.0}}}),
    ("T1 minimum R", "T1 >= 0.75R", {"trade": {"target_r": [0.75, 2.0, 3.0]}}),
    ("T1 minimum R", "T1 >= 1.5R", {"trade": {"target_r": [1.5, 2.0, 3.0]}}),
    # Mode comparisons the report asks for (section 11).
    ("contra mode", "dollar ($1/$2/$3)", {"selection": {"contra": {"mode": "dollar"}}}),
    ("contra RVOL", "min RVOL 1.2", {"selection": {"contra": {"min_rvol": 1.2}}}),
    ("entry_mode", "all confirmed_only", {"strategies": {"entry_mode": {"A": "confirmed_only", "C": "confirmed_only",
                                                                        "A0": "confirmed_only"}}}),
    ("entry_mode", "all aggressive_only", {"strategies": {"entry_mode": {"A": "aggressive_only", "B": "aggressive_only",
                                                                         "C": "aggressive_only", "A0": "aggressive_only"}}}),
    ("target_mode", "risk_multiple 2R/3R/4R", {"trade": {"target_mode": "risk_multiple"}}),
]


def summarize(t: pd.DataFrame) -> dict:
    if t.empty:
        return {"trades": 0}
    pnl = t["pnl"]
    gl = -pnl[pnl < 0].sum()
    out = {"trades": len(t), "win %": round((pnl > 0).mean() * 100, 1), "avg R": round(t["R"].mean(), 3),
           "profit factor": round(pnl[pnl > 0].sum() / gl, 2) if gl else np.nan, "net P&L $": round(pnl.sum())}
    for s in ["A", "B", "C", "D", "A0"]:
        sub = t[t["strategy"] == s]
        out[f"{s} avg R (n)"] = f"{sub['R'].mean():+.2f} ({len(sub)})" if len(sub) else "–"
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--every", type=int, default=4)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--start", default="2018-06-01")
    ap.add_argument("--only", nargs="*", help="labels to run (default: all)")
    a = ap.parse_args()
    base_meta = ROOT / "reports" / "backtest" / "base" / "run.json"
    if base_meta.exists():
        is_end = json.loads(base_meta.read_text())["in_sample_until"]
    else:
        d = [x for x in sessions("SPY") if x >= pd.Timestamp(a.start)]
        is_end = str(d[int(len(d) * 0.6)].date())
    out = ROOT / "reports" / "backtest" / "sweep"
    out.mkdir(parents=True, exist_ok=True)
    path = out / "summary.csv"
    done = pd.read_csv(path) if path.exists() else pd.DataFrame()
    rows = done.to_dict("records")
    for group, label, ov in VARIANTS:
        if a.only and label not in a.only:
            continue
        if len(done) and label in set(done["variant"]):
            continue
        name = "sweep_" + "".join(c if c.isalnum() else "_" for c in label)
        end = (pd.Timestamp(is_end) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        t = run(a.start, end, a.workers, name, overrides=ov, every=a.every, split_at="2100-01-01")
        rows.append({"group": group, "variant": label, "every": a.every, "period": f"{a.start}..{end}", **summarize(t)})
        pd.DataFrame(rows).to_csv(path, index=False)
        print(label, rows[-1])


if __name__ == "__main__":
    main()
