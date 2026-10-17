"""Messages between the strategies and whatever executes them (the backtest's
simulated broker or the Alpaca router). Strategies never touch a broker."""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field

import pandas as pd

_ids = itertools.count(1)


@dataclass
class Order:
    symbol: str
    side: str                    # "sell" (short entry) | "buy" (cover)
    kind: str                    # "stop" | "market"
    qty: int
    price: float | None          # stop trigger price
    valid_bars: int              # cancel after this many 1-minute bars unfilled
    strategy: str                # "A", "B", "C", "D", "A0"
    tag: str                     # "entry1", "entry2", "entry", "add"
    reason: str
    placed: pd.Timestamp | None = None
    oid: int = field(default_factory=lambda: next(_ids))


@dataclass
class Place:
    order: Order


@dataclass
class Cancel:
    oid: int
    reason: str


@dataclass
class MoveStop:
    """Set the protective buy-stop for the whole position (never raised)."""
    symbol: str
    price: float
    reason: str


@dataclass
class ExitAll:
    symbol: str
    reason: str


@dataclass
class Log:
    symbol: str
    text: str


@dataclass
class Fill:
    oid: int
    symbol: str
    side: str
    qty: int
    price: float
    time: pd.Timestamp
