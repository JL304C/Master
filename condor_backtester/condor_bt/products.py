"""Product definitions: SPX, XSP, ES, MES.

Everything inside the engine is expressed in **SPX-equivalent index points**
(strikes, premiums, widths). A product only changes:

- how many dollars one SPX point is worth (``usd_per_point``),
- how prices look on the broker screen (``price_scale``: XSP quotes are 1/10),
- the strike grid, which expirations are listed and how far out,
- how expiration is settled (AM/PM, spot index vs. futures),
- fees and the margin model.

Fee and margin numbers below are rough placeholders, not quotes from any
broker. Override them in the YAML (``product_overrides``) with your own.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any


@dataclass(frozen=True)
class ExpirationRule:
    kind: str       # "friday" | "third_friday" | "quarterly" | "month_end"
    max_dte: int    # how far out this series is listed (calendar days)


@dataclass(frozen=True)
class Product:
    name: str
    usd_per_point: float          # $ per 1 SPX-equivalent point, per contract
    price_scale: float            # native quote = SPX-equivalent points * price_scale
    strike_step: float            # strike grid, in SPX-equivalent points
    underlying_kind: str          # "index" (SPX/XSP) or "futures" (ES/MES)
    settlement: str               # "pm" | "am" | "am_on_third_friday" | "am_on_quarterly"
    exercise_style: str           # "european" | "american" (informational; see README)
    expiration_rules: tuple[ExpirationRule, ...]
    fee_per_contract_open: float  # $ per contract per leg (commission + exchange + clearing)
    fee_per_contract_close: float  # $ per contract per leg when closed before expiry
    settlement_fee: float         # $ per contract per leg that settles in the money
    margin_model: str             # "regt" (max loss) or "span_approx"
    span_scan_range: float = 0.08  # +/- underlying move scanned by span_approx
    span_vol_shift: float = 0.25   # relative IV shift scanned by span_approx
    span_min_margin_usd: float = 0.0  # floor per position for span_approx
    notes: str = ""

    def to_native(self, pts: float) -> float:
        return pts * self.price_scale

    def from_native(self, native: float) -> float:
        return native / self.price_scale


PRODUCTS: dict[str, Product] = {
    "SPX": Product(
        name="SPX", usd_per_point=100.0, price_scale=1.0, strike_step=5.0,
        underlying_kind="index", settlement="am_on_third_friday", exercise_style="european",
        expiration_rules=(ExpirationRule("friday", 120), ExpirationRule("third_friday", 400)),
        fee_per_contract_open=1.25, fee_per_contract_close=1.25, settlement_fee=0.0,
        margin_model="regt",
        notes="SPXW Friday weeklies (PM) + standard 3rd-Friday SPX (AM, SOQ).",
    ),
    "XSP": Product(
        name="XSP", usd_per_point=10.0, price_scale=0.1, strike_step=10.0,
        underlying_kind="index", settlement="pm", exercise_style="european",
        expiration_rules=(ExpirationRule("friday", 120), ExpirationRule("third_friday", 400)),
        fee_per_contract_open=0.75, fee_per_contract_close=0.75, settlement_fee=0.0,
        margin_model="regt",
        notes="Mini-SPX = SPX/10, $100 multiplier, $1 strikes (= 10 SPX points), cash settled.",
    ),
    "ES": Product(
        name="ES", usd_per_point=50.0, price_scale=1.0, strike_step=5.0,
        underlying_kind="futures", settlement="am_on_quarterly", exercise_style="american",
        expiration_rules=(ExpirationRule("third_friday", 400), ExpirationRule("friday", 35)),
        fee_per_contract_open=2.00, fee_per_contract_close=2.00, settlement_fee=0.0,
        margin_model="span_approx", span_min_margin_usd=500.0,
        notes="Quarterlies settle into expiring futures (cash, SOQ). Serial months exercise "
              "into the next quarterly future (a real futures position). American style.",
    ),
    "MES": Product(
        name="MES", usd_per_point=5.0, price_scale=1.0, strike_step=5.0,
        underlying_kind="futures", settlement="pm", exercise_style="european",
        # New financially settled Micro options (launched 2026-06-29): weekday expiries,
        # ~8 consecutive Fridays listed. Raise max_dte if CME lists longer dates.
        expiration_rules=(ExpirationRule("friday", 56), ExpirationRule("month_end", 63)),
        fee_per_contract_open=0.85, fee_per_contract_close=0.85, settlement_fee=0.0,
        margin_model="span_approx", span_min_margin_usd=50.0,
        notes="Financially settled at the 3:00pm CT ES fixing price, European style.",
    ),
}


def get_product(name: str, overrides: dict[str, Any] | None = None) -> Product:
    try:
        base = PRODUCTS[name.upper()]
    except KeyError as exc:
        raise ValueError(f"unknown product {name!r}; choose from {sorted(PRODUCTS)}") from exc
    if not overrides:
        return base
    overrides = dict(overrides)
    if "expiration_rules" in overrides:
        overrides["expiration_rules"] = tuple(
            r if isinstance(r, ExpirationRule) else ExpirationRule(r["kind"], int(r["max_dte"]))
            for r in overrides["expiration_rules"]
        )
    unknown = set(overrides) - set(base.__dataclass_fields__)
    if unknown:
        raise ValueError(f"unknown product_overrides keys: {sorted(unknown)}")
    return replace(base, **overrides)
