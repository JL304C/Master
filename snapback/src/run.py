"""Variant A backtest driver.

    python -m src.run --estimate         # Databento price for the data not yet cached
    python -m src.run --download         # download and cache (after you approve the price)
    python -m src.run                    # backtest from the cache, write results/
    python -m src.run --csv NQ=nq_5m.csv # backtest a CSV from another vendor instead
"""
from __future__ import annotations

import argparse
import itertools
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from . import data, report
from .backtest import Instrument, detect, run
from .box import build_market

ROOT = Path(__file__).resolve().parent.parent
ENTRY_KEYS = ("box_open_time", "box_tz", "skip_pct", "skip_lookback", "entry_mode", "max_wait_bars",
              "touch_depth", "require_turn_candle", "allow_reentry_after_close_inside")


def expand(spec: dict) -> list[dict]:
    """Grid spec -> list of override dicts. List-of-dict values are bundled overrides."""
    axes = []
    for k, vals in spec.items():
        axes.append([v if isinstance(v, dict) else {k: v} for v in vals])
    return [dict(kv for d in combo for kv in d.items()) for combo in itertools.product(*axes)]


def variations(spec: dict) -> list[tuple[str, dict]]:
    out = []
    for k, vals in spec.items():
        for v in vals:
            ov = v if isinstance(v, dict) else {k: v}
            out.append((", ".join(f"{a}={b}" for a, b in ov.items()), ov))
    return out


class Lab:
    """Caches markets and setup detection so parameter sweeps only re-run the exits."""

    def __init__(self, bars5, inst, acct):
        self.bars5, self.inst, self.acct = bars5, inst, acct
        self._m, self._d = {}, {}

    def market(self, p):
        off = int(p["box_open_time"].split(":")[1])
        if off not in self._m:
            self._m[off] = build_market(self.bars5, off)
        return self._m[off]

    def run(self, p, rng=None):
        key = tuple(p[k] for k in ENTRY_KEYS)
        m = self.market(p)
        if key not in self._d:
            self._d[key] = detect(m, p)[1]
        return run(m, p, self.inst, self.acct, self._d[key], rng)


def split(df, date):
    if df.empty:
        return df, df
    return df[df["date"] < date], df[df["date"] >= date]


def label(ov: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in ov.items())


def backtest_instrument(name, bars5, cfg, outdir: Path) -> tuple[str, dict]:
    icfg = cfg["instruments"][name]
    inst = Instrument(name, icfg["point_value"], icfg["tick_size"], icfg["commission_per_side"], icfg["slippage_ticks"])
    acct, p0, val = cfg["account"], cfg["strategy"], cfg["validation"]
    lab = Lab(bars5, inst, acct)
    risk = acct["risk_per_trade"]

    trades, days = lab.run(p0)
    dates = np.sort(days["date"].unique())
    split_date = pd.Timestamp(dates[int(len(dates) * val["in_sample_frac"])])
    t_is, t_oos = split(trades, split_date)
    d_is, d_oos = split(days, split_date)
    s_is, s_oos = report.stats(t_is, d_is, risk), report.stats(t_oos, d_oos, risk)

    trades.to_csv(outdir / f"trades_{name}.csv", index=False)
    days.to_csv(outdir / f"days_{name}.csv", index=False)
    report.equity_chart(trades, split_date, f"{name}: Snapback Variant A, confirmed rules", outdir / f"equity_{name}.png", risk)

    # ---- baselines (out-of-sample) ----
    rows = {"Strategy (confirmed rules)": s_oos}
    bo, bo_days = lab.run({**p0, "entry_mode": "breakout"})
    rows["Immediate breakout entry"] = report.stats(split(bo, split_date)[1], split(bo_days, split_date)[1], risk)
    rand_exp = []
    for seed in range(val["random_seeds"]):
        rt, _ = lab.run(p0, np.random.default_rng(seed))
        rt = split(rt, split_date)[1]
        rand_exp.append(rt["R"].mean() if len(rt) else np.nan)
    rand_exp = np.array(rand_exp)
    rows["Random direction (mean of seeds)"] = {"trades": len(t_oos), "expectancy_R": np.nanmean(rand_exp)}
    p_random = float(np.mean(rand_exp >= s_oos.get("expectancy_R", np.inf)))
    bh_is = report.buy_and_hold(bars5, bars5.index[0], split_date.tz_localize(bars5.index.tz))
    bh_oos = report.buy_and_hold(bars5, split_date.tz_localize(bars5.index.tz), bars5.index[-1] + pd.Timedelta(minutes=5))

    # ---- in-sample grid search, then out-of-sample check of the pick ----
    combos = expand(cfg["grid"])
    grid_res = []
    for ov in combos:
        t, _ = lab.run({**p0, **ov})
        ti, to = split(t, split_date)
        grid_res.append((ov, t, ti["R"].mean() if len(ti) >= val["min_trades_for_selection"] else -np.inf))
    best_ov, best_t, _ = max(grid_res, key=lambda x: x[2])
    bt_is, bt_oos = split(best_t, split_date)
    _, bd = lab.run({**p0, **best_ov})
    bd_is, bd_oos = split(bd, split_date)
    sel_is, sel_oos = report.stats(bt_is, bd_is, risk), report.stats(bt_oos, bd_oos, risk)

    # ---- walk-forward: pick the best grid combo on each train window, trade the next test window ----
    wf_parts, wf_log = [], []
    start = pd.Timestamp(dates[0])
    train = pd.DateOffset(years=val["walk_forward_train_years"])
    step = pd.DateOffset(months=val["walk_forward_test_months"])
    t0 = start + train
    while t0 < pd.Timestamp(dates[-1]):
        t1 = t0 + step
        best = None
        for ov, t, _ in grid_res:
            tr = t[(t["date"] >= t0 - train) & (t["date"] < t0)]
            if len(tr) >= val["min_trades_for_selection"] // 2:
                e = tr["R"].mean()
                if best is None or e > best[0]:
                    best = (e, ov, t)
        if best:
            te = best[2][(best[2]["date"] >= t0) & (best[2]["date"] < t1)]
            wf_parts.append(te)
            wf_log.append({"test_start": t0.date(), "test_end": t1.date(), "train_exp_R": best[0],
                           "test_trades": len(te), "test_exp_R": te["R"].mean() if len(te) else np.nan,
                           "params": label(best[1])})
        t0 = t1
    wf = pd.concat(wf_parts) if wf_parts else pd.DataFrame(columns=trades.columns)
    s_wf = report.stats(wf, None, risk)

    # ---- sensitivity: one parameter at a time ----
    sens = {"confirmed rules": {"IS exp R": s_is.get("expectancy_R"), "IS PF": s_is.get("profit_factor"),
                                "IS trades": s_is["trades"], "OOS exp R": s_oos.get("expectancy_R"),
                                "OOS PF": s_oos.get("profit_factor"), "OOS trades": s_oos["trades"],
                                "OOS win %": s_oos.get("win_rate")}}
    for name_v, ov in variations(cfg["sensitivity"]):
        t, d = lab.run({**p0, **ov})
        ti, to = split(t, split_date)
        di, do = split(d, split_date)
        a, b = report.stats(ti, di, risk), report.stats(to, do, risk)
        sens[name_v] = {"IS exp R": a.get("expectancy_R"), "IS PF": a.get("profit_factor"), "IS trades": a["trades"],
                        "OOS exp R": b.get("expectancy_R"), "OOS PF": b.get("profit_factor"), "OOS trades": b["trades"],
                        "OOS win %": b.get("win_rate")}
    pd.DataFrame(sens).T.to_csv(outdir / f"sensitivity_{name}.csv")

    # ---- heatmap stop_k x be_trigger (in-sample) ----
    hm = cfg["heatmap"]
    grid = pd.DataFrame(index=hm["stop_k"], columns=[str(b) for b in hm["be_trigger"]], dtype=float)
    for k in hm["stop_k"]:
        for b in hm["be_trigger"]:
            t, _ = lab.run({**p0, "stop_k": k, "be_trigger": b})
            ti = split(t, split_date)[0]
            grid.loc[k, str(b)] = ti["R"].mean() if len(ti) else np.nan
    report.heatmap_chart(grid, f"{name}: in-sample expectancy (R per trade)", outdir / f"heatmap_{name}.png")

    # ---- pass criteria (confirmed rules, out-of-sample) ----
    one_step = [v for k, v in sens.items() if k != "confirmed rules" and k.split("=")[0] in ("stop_k", "be_trigger", "touch_depth", "max_wait_bars", "be_buffer")]
    collapse = any((v["OOS exp R"] is None or not np.isfinite(v["OOS exp R"]) or v["OOS exp R"] <= 0) for v in one_step)
    checks = {
        "Positive OOS expectancy after costs": s_oos.get("expectancy_R", -1) > 0,
        "OOS profit factor >= 1.3": s_oos.get("profit_factor", 0) >= 1.3,
        "At least ~100 OOS trades": s_oos["trades"] >= 100,
        "No collapse when one parameter moves one step (OOS exp > 0 for each)": not collapse,
        "Beats random direction (p < 0.05)": p_random < 0.05,
        "Not driven by a few huge days (exp. without top 5 > 0)": s_oos.get("expectancy_without_top5_R", -1) > 0,
    }

    cols = ["trades", "win_rate", "breakeven_win_rate", "avg_win_R", "avg_loss_R", "expectancy_R", "profit_factor",
            "max_dd_pct", "longest_losing_streak", "sharpe", "no_setup_pct"]
    long_short = {f"{d} (OOS)": report.stats(t_oos[t_oos.direction == d], None, risk) for d in ("long", "short")}
    md = [f"## {name}\n",
          f"Sessions: {len(dates)} ({pd.Timestamp(dates[0]).date()} → {pd.Timestamp(dates[-1]).date()}); "
          f"in-sample before {split_date.date()}, out-of-sample after. "
          f"Days skipped for contract roll: {(days.outcome == 'roll').sum()}. "
          f"Trades whose first target was already at or behind the entry price (long snapback candle): "
          f"{int((trades.get('first_target_R', pd.Series(dtype=float)) <= 0).sum())}.\n",
          "### Confirmed rules\n",
          report.fmt_table({"In-sample": s_is, "Out-of-sample": s_oos, **long_short}, cols),
          f"\n**Win rate vs break-even:** OOS win rate {s_oos.get('win_rate', float('nan')):.1f}% against the "
          f"{s_oos.get('breakeven_win_rate', float('nan')):.1f}% needed to break even at these average win/loss sizes.\n",
          "\n### Baselines (out-of-sample)\n",
          report.fmt_table(rows, ["trades", "win_rate", "expectancy_R", "profit_factor", "sharpe"]),
          f"\nRandom-direction: {np.mean(rand_exp >= s_oos.get('expectancy_R', np.inf)) * 100:.0f}% of "
          f"{len(rand_exp)} coin-flip runs did as well or better (p = {p_random:.2f}).\n",
          f"\nBuy-and-hold 1x: in-sample {bh_is.get('return_pct', float('nan')):.1f}% (Sharpe {bh_is.get('sharpe', float('nan')):.2f}), "
          f"out-of-sample {bh_oos.get('return_pct', float('nan')):.1f}% (Sharpe {bh_oos.get('sharpe', float('nan')):.2f}, "
          f"max DD {bh_oos.get('max_dd_pct', float('nan')):.1f}%).\n",
          "\n### In-sample grid pick, checked out-of-sample\n",
          f"Best of {len(combos)} exit combinations in-sample: `{label(best_ov)}`\n\n",
          report.fmt_table({"In-sample (picked here)": sel_is, "Out-of-sample": sel_oos}, cols),
          "\n### Walk-forward\n",
          report.fmt_table({"Walk-forward (stitched test windows)": s_wf}, ["trades", "win_rate", "expectancy_R", "profit_factor", "max_dd_pct"]),
          "\n", report.df_md(pd.DataFrame(wf_log)),
          "\n### Sensitivity (one change at a time)\n",
          report.df_md(pd.DataFrame(sens).T),
          "\n### By year (confirmed rules, full period)\n", report.df_md(report.breakdown(trades, "year")),
          "\n### By weekday (confirmed rules, full period)\n", report.df_md(report.breakdown(trades, "weekday")),
          "\n### Pass criteria\n",
          "".join(f"- {'PASS' if ok else 'FAIL'}: {k}\n" for k, ok in checks.items()),
          f"\n![equity](equity_{name}.png)\n\n![heatmap](heatmap_{name}.png)\n"]
    return "".join(md), checks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--estimate", action="store_true")
    ap.add_argument("--download", action="store_true")
    ap.add_argument("--csv", action="append", default=[], help="NAME=path to 1- or 5-minute OHLC CSV")
    ap.add_argument("--only", nargs="*", help="instrument names to run")
    ap.add_argument("--out", default=str(ROOT / "results"))
    args = ap.parse_args()

    cfg = yaml.safe_load(open(args.config))
    dcfg = cfg["data"]
    cache = ROOT / dcfg["cache_dir"]
    names = args.only or list(cfg["instruments"])

    def dcfg_for(n):
        return {**dcfg, **{k: v for k, v in cfg["instruments"][n].items() if k in ("start", "end")}}

    if args.estimate:
        print("Databento cost estimate for uncached data:")
        total = sum(data.estimate_cost([cfg["instruments"][n]["symbol"]], dcfg_for(n), cache) for n in names)
        print(f"TOTAL: ${total:,.2f}")
        return
    if args.download:
        for n in names:
            data.download(cfg["instruments"][n]["symbol"], dcfg_for(n), cache)
        return

    csvs = dict(c.split("=", 1) for c in args.csv)
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    sections, verdicts = [], {}
    for n in names:
        raw = data.load_csv(csvs[n]) if n in csvs else data.load_1m(cfg["instruments"][n]["symbol"], dcfg_for(n), cache)
        bars5 = data.resample(raw, "5min")
        print(f"{n}: {len(bars5):,} five-minute bars, running...")
        md, checks = backtest_instrument(n, bars5, cfg, outdir)
        sections.append(md)
        verdicts[n] = checks

    lines = ["# Snapback Variant A: backtest report\n",
             "All R figures are after commission and slippage. Win rate is compared with the break-even win rate "
             "implied by the average win and loss.\n",
             "\n## Verdict\n"]
    for n, checks in verdicts.items():
        ok = all(checks.values())
        failed = [k for k, v in checks.items() if not v]
        lines.append(f"- **{n}: {'PASSES' if ok else 'does not pass'}** the pre-paper-trading criteria"
                     + ("." if ok else f". Failed: {'; '.join(failed)}.") + "\n")
    lines.append("\n")
    (outdir / "summary.md").write_text("".join(lines) + "\n".join(sections))
    print(f"Wrote {outdir / 'summary.md'}")


if __name__ == "__main__":
    main()
