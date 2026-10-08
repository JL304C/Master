"""Parameter sweeps and product comparisons."""
from __future__ import annotations

import itertools
from concurrent.futures import ProcessPoolExecutor

import pandas as pd

from .config import Config
from .data import MarketData
from .engine import Backtester
from .metrics import summarize
from .products import get_product

SPEC_GRID = {
    "debit_delta": [0.15, 0.20, 0.25],
    "credit_delta": [0.06, 0.08, 0.10, 0.12],
    "credit_width": [50, 75, 100],
    "min_credit": [0.01, 1.0, 2.0],
}

# Model-risk grid: how much do results move if the synthetic skew is wrong?
SURFACE_GRID = {
    "surface.vix_to_atm": [0.80, 0.87, 0.95],
    "surface.skew_put": [0.25, 0.36, 0.45],
}

KEEP = ["trades", "skipped", "win_rate", "losses", "full_max_loss_trades", "avg_credit_pts",
        "total_pnl_mtm_usd", "fees_usd", "worst_trade_usd", "max_drawdown_mtm_usd",
        "max_concurrent_positions", "worst_case_aggregate_loss_usd", "peak_buying_power_usd",
        "roc_regt_pct", "cagr_on_regt_capital_pct", "roc_peak_bp_pct"]


def _run_one(args):
    cfg, market, chains, label = args
    try:
        res = Backtester(cfg, market, chains).run()
        s = summarize(res)
        return {**label, **{k: s[k] for k in KEEP}}
    except Exception as exc:  # keep the sweep going; report the failure in the table
        return {**label, "error": str(exc)}


def run_grid(cfg: Config, market: MarketData, grid: dict, chains: str | None = None,
             jobs: int = 1) -> pd.DataFrame:
    keys = list(grid)
    tasks = []
    for combo in itertools.product(*(grid[k] for k in keys)):
        label = dict(zip(keys, combo))
        tasks.append((cfg.with_(**label), market, chains, label))
    if jobs > 1:
        with ProcessPoolExecutor(max_workers=jobs) as ex:
            rows = list(ex.map(_run_one, tasks))
    else:
        rows = [_run_one(t) for t in tasks]
    return pd.DataFrame(rows)


def compare_configs(cfgs: list[Config], market: MarketData, jobs: int = 1) -> pd.DataFrame:
    """Run several configs (typically one per product) side by side."""
    tasks = []
    for c in cfgs:
        prod = get_product(c.product, c.product_overrides)
        label = {"product": prod.name, "target_dte": c.target_dte, "debit_width": c.debit_width,
                 "credit_width": c.credit_width, "usd_per_spx_point": prod.usd_per_point}
        tasks.append((c, market, None, label))
    if jobs > 1:
        with ProcessPoolExecutor(max_workers=jobs) as ex:
            rows = list(ex.map(_run_one, tasks))
    else:
        rows = [_run_one(t) for t in tasks]
    return pd.DataFrame(rows)


def to_markdown(df: pd.DataFrame) -> str:
    cols = list(df.columns)
    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in df.itertuples(index=False):
        out.append("| " + " | ".join("" if pd.isna(v) else (f"{v:,.2f}" if isinstance(v, float) else str(v))
                                     for v in r) + " |")
    return "\n".join(out) + "\n"
