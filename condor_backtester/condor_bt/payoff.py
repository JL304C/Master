"""Payoff math for the broken-wing put condor (spec section 5).

Legs (all puts, same expiration), strikes L1 > L2 >= L3 > L4:
    L1 long, L2 short   -> put debit spread (the "tent")
    L3 short, L4 long   -> put credit spread (the broken wing)

All values are in points per 1 contract; multiply by the product's
``usd_per_point`` for dollars.
"""
from __future__ import annotations

from typing import NamedTuple


class Strikes(NamedTuple):
    l1: float
    l2: float
    l3: float
    l4: float


def put_intrinsic(strike: float, price: float) -> float:
    return max(strike - price, 0.0)


def condor_settlement_value(price: float, k: Strikes) -> float:
    """Value of the position at expiration (what you hold, before the entry credit)."""
    debit_spread = put_intrinsic(k.l1, price) - put_intrinsic(k.l2, price)
    credit_spread = put_intrinsic(k.l3, price) - put_intrinsic(k.l4, price)
    return debit_spread - credit_spread


def condor_pnl_at_expiry(price: float, k: Strikes, credit: float) -> float:
    """P&L at expiration in points: entry credit plus settlement value."""
    return credit + condor_settlement_value(price, k)


def max_profit(k: Strikes, credit: float) -> float:
    return (k.l1 - k.l2) + credit


def max_loss(k: Strikes, credit: float) -> float:
    """Positive number: the most the position can lose (spec: W - D - C)."""
    return (k.l3 - k.l4) - (k.l1 - k.l2) - credit


def outcome_bucket(price: float, k: Strikes) -> str:
    if price >= k.l1:
        return "above"
    if price >= k.l2:
        return "partial_upper"
    if price >= k.l3:
        return "tent"
    if price > k.l4:
        return "partial_lower"
    return "max_loss"
