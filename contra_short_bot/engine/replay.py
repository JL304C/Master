"""Replay days through the full backtest day loop and write the decision log,
the trades and 1-minute charts of each trade, for checking setups by eye.

    python -m engine.replay --day 2024-03-08
    python -m engine.replay --start 2024-03-04 --end 2024-03-15 --charts
"""
from __future__ import annotations

import argparse

import pandas as pd

from backtest.runner import run_day
from data.bars import sessions
from data.fetch import ROOT, load_config


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--charts", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    cfg = load_config()
    days = [pd.Timestamp(args.day)] if args.day else [
        d for d in sessions("SPY") if pd.Timestamp(args.start) <= d <= pd.Timestamp(args.end)]
    out = ROOT / "reports" / "replay"
    out.mkdir(parents=True, exist_ok=True)
    all_trades = []
    for d in days:
        trades, logs, traders = run_day(d, cfg, keep_traders=args.charts)
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
        print(t[["date", "symbol", "strategy", "entry_time", "entry", "adds", "targets_hit", "exit_reason", "R",
                 "mfe_R"]].to_string(index=False))


if __name__ == "__main__":
    main()
