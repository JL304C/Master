"""Command line entry point.

    python -m condor_bt run      --config configs/spx.yaml --market data/market.csv
    python -m condor_bt sweep    --config configs/spx.yaml --market data/market.csv [--grid spec|surface]
    python -m condor_bt compare  --config configs/spx.yaml --config configs/es.yaml \
                                 --config configs/mes.yaml --config configs/xsp.yaml --market data/market.csv
    python -m condor_bt calibrate --config configs/xsp.yaml --market data/market.csv --chains real.csv
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import yaml

from .config import load_config
from .data import MarketData


def _parse_sets(pairs: list[str]) -> dict:
    out = {}
    for p in pairs or []:
        k, _, v = p.partition("=")
        out[k.strip()] = yaml.safe_load(v)
    return out


def _load(args, path=None):
    cfg = load_config(path or args.config)
    sets = _parse_sets(args.set)
    if sets:
        cfg = cfg.with_(**sets)
    market = MarketData.from_csv(args.market, default_rate=cfg.risk_free_rate)
    return cfg, market


def cmd_run(args):
    from .engine import Backtester
    from .report import write_report
    cfg, market = _load(args)
    res = Backtester(cfg, market, args.chains).run()
    out = args.out or os.path.join("results", f"{cfg.product.lower()}_{cfg.target_dte}dte")
    summary = write_report(res, out)
    with open(os.path.join(out, "summary.md")) as fh:
        print(fh.read())
    print(f"outputs written to {out}/")
    return summary


def cmd_sweep(args):
    from .sweep import SPEC_GRID, SURFACE_GRID, run_grid, to_markdown
    cfg, market = _load(args)
    grid = SURFACE_GRID if args.grid == "surface" else SPEC_GRID
    df = run_grid(cfg, market, grid, args.chains, jobs=args.jobs)
    out = args.out or os.path.join("results", f"sweep_{args.grid}_{cfg.product.lower()}")
    os.makedirs(out, exist_ok=True)
    df.to_csv(os.path.join(out, "sweep.csv"), index=False)
    with open(os.path.join(out, "sweep.md"), "w") as fh:
        fh.write(to_markdown(df))
    print(to_markdown(df))
    print(f"outputs written to {out}/")


def cmd_compare(args):
    from .sweep import compare_configs, to_markdown
    loaded = [_load(args, path) for path in args.config]
    market = loaded[0][1]
    df = compare_configs([c for c, _ in loaded], market, jobs=args.jobs)
    out = args.out or os.path.join("results", "compare")
    os.makedirs(out, exist_ok=True)
    df.to_csv(os.path.join(out, "compare.csv"), index=False)
    with open(os.path.join(out, "compare.md"), "w") as fh:
        fh.write(to_markdown(df))
    print(to_markdown(df))
    print(f"outputs written to {out}/")


def cmd_calibrate(args):
    from .calibrate import collect_points, fit
    from .chains import CsvChain
    from .products import get_product
    cfg, market = _load(args)
    if not args.chains:
        sys.exit("calibrate needs --chains (a real option chain CSV)")
    prod = get_product(cfg.product, cfg.product_overrides)
    pts = collect_points(CsvChain(args.chains, prod, market, cfg.div_yield), prod, market, cfg.div_yield)
    if not pts:
        sys.exit("no usable puts (need 20-150 DTE, |delta| 0.05-0.50, VIX on those dates)")
    sp, rmse = fit(pts, cfg.surface)
    print(f"fitted on {len(pts)} puts, IV RMSE {100 * rmse:.2f} vol points")
    print(yaml.safe_dump({"surface": {"vix_to_atm": round(sp.vix_to_atm, 3),
                                      "skew_put": round(sp.skew_put, 3),
                                      "curv_put": round(sp.curv_put, 3)}}, sort_keys=False))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="condor_bt", description="Broken-wing put condor backtester")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("run", cmd_run), ("sweep", cmd_sweep), ("compare", cmd_compare), ("calibrate", cmd_calibrate)):
        sp = sub.add_parser(name)
        sp.add_argument("--config", required=True, action="append" if name == "compare" else "store",
                        help="YAML config" + (" (repeat once per product)" if name == "compare" else ""))
        sp.add_argument("--market", required=True, help="market CSV (scripts/fetch_market_data.py)")
        sp.add_argument("--chains", help="real option chain CSV; omit to use model prices")
        sp.add_argument("--out")
        sp.add_argument("--set", action="append", metavar="KEY=VALUE",
                        help="override a config value, e.g. --set target_dte=49 --set surface.skew_put=0.3")
        sp.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 1))
        sp.set_defaults(fn=fn)
        if name == "sweep":
            sp.add_argument("--grid", choices=["spec", "surface"], default="spec")
    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
