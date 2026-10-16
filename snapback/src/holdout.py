"""Out-of-time test of ONE frozen rule on data the research never touched.

Frozen rule (fixed before running, from config.yaml variant_d + eod_exit false):
  NQ, 09:00-10:00 ET box; long on a 5-min close above it / short below, entries 10:00-12:00 ET,
  one trade a day; stop at the opposite side of the box; target entry + 2 x box height;
  no breakeven, no trailing; held until target or stop (overnight and weekends allowed).

    python -m src.holdout     # writes results/holdout_nq_2015_2019.md
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from . import data, report
from .backtest import Instrument, run
from .box import build_market

ROOT = Path(__file__).resolve().parent.parent
START, END = "2015-01-01", "2020-01-01"


def main():
    cfg = yaml.safe_load(open(ROOT / "config.yaml"))
    p = {**cfg["strategy"], **cfg["variant_b"]["strategy"], **cfg["variant_d"]["strategy"], "eod_exit": False}
    ic = cfg["instruments"]["NQ"]
    inst = Instrument("NQ", ic["point_value"], ic["tick_size"], ic["commission_per_side"], ic["slippage_ticks"])
    p["tick_size"] = inst.tick_size
    risk = cfg["account"]["risk_per_trade"]

    bars5 = data.resample(data.load_1m(ic["symbol"], {**cfg["data"], "start": START, "end": END}, ROOT / "data"), "5min")
    bars5 = bars5[bars5.index < pd.Timestamp(END, tz=bars5.index.tz)]
    m = build_market(bars5)

    t, d = run(m, p, inst, cfg["account"])
    s = report.stats(t, d, risk)
    rand = np.array([run(m, p, inst, cfg["account"], flip_rng=np.random.default_rng(k))[0]["R"].mean() for k in range(50)])
    p_rand = float(np.mean(rand >= s["expectancy_R"]))
    ti, _ = run(m, {**p, "eod_exit": True}, inst, cfg["account"])
    intraday = report.stats(ti, d, risk)
    bh = report.buy_and_hold(bars5, bars5.index[0], bars5.index[-1] + pd.Timedelta(minutes=5))
    sides = {f"{x}": report.stats(t[t.direction == x], None, risk) for x in ("long", "short")}
    gaps = t[t.exit_reason == "stop_gap"]

    checks = {
        "Positive expectancy after costs": s["expectancy_R"] > 0,
        "Profit factor >= 1.3": s["profit_factor"] >= 1.3,
        "At least ~100 trades": s["trades"] >= 100,
        "Beats random direction (p < 0.05)": p_rand < 0.05,
        "Not driven by a few huge days (exp. without top 5 > 0)": s["expectancy_without_top5_R"] > 0,
    }
    cols = ["trades", "win_rate", "breakeven_win_rate", "avg_win_R", "avg_loss_R", "expectancy_R",
            "profit_factor", "return_pct", "max_dd_pct", "longest_losing_streak", "sharpe"]
    md = ["# Hold-out test: NQ breakout held overnight, 2015-2019\n",
          "One frozen rule, data never used in the research (2020-2026). Nothing was tuned on this period.\n\n",
          "## Verdict\n", "".join(f"- {'PASS' if ok else 'FAIL'}: {k}\n" for k, ok in checks.items()),
          "\n## Results\n",
          report.fmt_table({"Frozen rule (hold to target/stop)": s, **sides,
                            "Same entries, flat at 16:00": intraday}, cols),
          f"\nRandom direction: {p_rand * 100:.0f}% of 50 coin-flip runs did as well or better "
          f"(mean {np.nanmean(rand):+.3f}R).\n",
          f"\nBuy-and-hold NQ 1x over the period: {bh['return_pct']:.1f}% (Sharpe {bh['sharpe']:.2f}, max DD {bh['max_dd_pct']:.1f}%).\n",
          f"\nGaps through the stop: {len(gaps)} trades"
          + (f", average {gaps.R.mean():.2f}R, worst {gaps.R.min():.2f}R" if len(gaps) else "") + ". "
          f"Worst trade overall {t.R.min():.2f}R.\n",
          "\n## By year\n", report.df_md(report.breakdown(t, "year")),
          "\n![equity](holdout_equity_nq.png)\n"]
    out = ROOT / "results"
    t.to_csv(out / "holdout_trades_nq_2015_2019.csv", index=False)
    report.equity_chart(t, pd.Timestamp(END), "NQ breakout held overnight: hold-out 2015-2019", out / "holdout_equity_nq.png", risk)
    (out / "holdout_nq_2015_2019.md").write_text("".join(md))
    print("".join(md))


if __name__ == "__main__":
    main()
