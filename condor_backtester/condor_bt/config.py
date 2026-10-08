"""Run configuration (one YAML file per run, spec section 11)."""
from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from datetime import date
from typing import Any

import yaml

from .pricing import SurfaceParams

WEEKDAYS = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4}


@dataclass(frozen=True)
class Config:
    product: str = "SPX"
    product_overrides: dict = field(default_factory=dict)

    entry_weekday: str = "FRI"
    target_dte: int = 90
    dte_tolerance: int = 7

    # Strategy. Widths and min_credit are in SPX-equivalent points for every
    # product, so ES/MES/XSP runs are directly comparable to the SPX spec.
    debit_delta: float = 0.20
    debit_width: float = 25.0
    credit_delta: float = 0.10
    credit_width: float = 100.0
    min_credit: float = 0.01
    overlap_rule: str = "skip"     # "skip" (L3 >= L2), "allow_equal" (L3 > L2), "shift" (move L3 below L2)

    # Fills. slippage_per_leg is in NATIVE quote units (what the broker shows:
    # 0.05 on XSP = $5/contract; 0.05 on MES = $0.25/contract).
    pricing: str = "mid"           # "mid" or "slippage"
    slippage_per_leg: float = 0.05

    take_profit_pct: float | None = None
    stop_loss_mult: float | None = None
    exit_dte: int | None = None

    max_concurrent_positions: int | None = None
    max_aggregate_max_loss: float | None = None   # dollars

    contracts: int = 1
    start_date: date = date(2020, 10, 1)
    end_date: date = date(2025, 10, 1)

    risk_free_rate: float = 0.04   # used where the market CSV has no rate
    div_yield: float = 0.015
    surface: SurfaceParams = field(default_factory=SurfaceParams)

    @property
    def weekday(self) -> int:
        return WEEKDAYS[self.entry_weekday.upper()[:3]]

    def with_(self, **kw: Any) -> "Config":
        surf = {k[len("surface."):]: kw.pop(k) for k in list(kw) if k.startswith("surface.")}
        cfg = replace(self, **kw)
        if surf:
            cfg = replace(cfg, surface=replace(cfg.surface, **surf))
        return cfg


def load_config(path: str, **overrides: Any) -> Config:
    with open(path) as fh:
        raw = yaml.safe_load(fh) or {}
    raw.update({k: v for k, v in overrides.items() if v is not None})
    known = {f.name for f in fields(Config)}
    # accept the spec's "underlying"/"multiplier" keys as aliases
    if "underlying" in raw and "product" not in raw:
        raw["product"] = raw.pop("underlying")
    raw.pop("underlying", None)
    if "multiplier" in raw:
        raise ValueError("'multiplier' is set by 'product' (SPX $100, ES $50, XSP $10 and MES $5 "
                         "per SPX point); remove it or use product_overrides.usd_per_point")
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"unknown config keys: {sorted(unknown)}")
    if "surface" in raw:
        raw["surface"] = SurfaceParams(**(raw["surface"] or {}))
    for k in ("start_date", "end_date"):
        if k in raw and isinstance(raw[k], str):
            raw[k] = date.fromisoformat(raw[k])
    return Config(**raw)
