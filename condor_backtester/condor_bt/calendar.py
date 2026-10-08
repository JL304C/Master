"""Expiration calendars.

Expirations are generated from rules (every Friday, 3rd Friday, quarterly,
month end). When a nominal expiration falls on a market holiday that is
inside the loaded data, it moves to the prior trading day.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import date, timedelta

from .products import Product


@dataclass(frozen=True)
class Expiration:
    date: date              # actual (holiday-adjusted) expiration date
    third_friday: bool      # nominal date is the month's 3rd Friday
    quarterly: bool         # nominal date is a Mar/Jun/Sep/Dec 3rd Friday


def third_friday(year: int, month: int) -> date:
    d = date(year, month, 15)
    return d + timedelta(days=(4 - d.weekday()) % 7)


def last_weekday_of_month(year: int, month: int) -> date:
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    d = nxt - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def next_quarterly(d: date) -> date:
    """First quarterly 3rd Friday on or after ``d``."""
    y, m = d.year, d.month
    while True:
        if m in (3, 6, 9, 12):
            tf = third_friday(y, m)
            if tf >= d:
                return tf
        m += 1
        if m > 12:
            y, m = y + 1, 1


class TradingCalendar:
    def __init__(self, trading_days: list[date]):
        self.days = sorted(trading_days)
        self._set = set(self.days)
        self.first, self.last = self.days[0], self.days[-1]

    def is_trading_day(self, d: date) -> bool:
        return d in self._set

    def on_or_before(self, d: date) -> date | None:
        i = bisect.bisect_right(self.days, d)
        return self.days[i - 1] if i else None

    def adjust(self, d: date) -> date:
        """Holiday-adjust inside the data range; leave dates past the data alone."""
        if d > self.last or d < self.first or d in self._set:
            return d
        prior = self.on_or_before(d)
        return prior if prior is not None else d

    def between(self, start: date, end: date) -> list[date]:
        lo = bisect.bisect_left(self.days, start)
        hi = bisect.bisect_right(self.days, end)
        return self.days[lo:hi]


def listed_expirations(product: Product, today: date, cal: TradingCalendar) -> list[Expiration]:
    """Expirations listed on ``today`` (strictly after today), across all rules."""
    found: dict[date, Expiration] = {}
    for rule in product.expiration_rules:
        horizon = today + timedelta(days=rule.max_dte)
        for nominal in _nominal_dates(rule.kind, today, horizon):
            actual = cal.adjust(nominal)
            if actual <= today:
                continue
            tf = nominal == third_friday(nominal.year, nominal.month)
            q = tf and nominal.month in (3, 6, 9, 12)
            prev = found.get(actual)
            found[actual] = Expiration(actual, tf or (prev.third_friday if prev else False),
                                       q or (prev.quarterly if prev else False))
    return sorted(found.values(), key=lambda e: e.date)


def _nominal_dates(kind: str, start: date, end: date):
    if kind == "friday":
        d = start + timedelta(days=(4 - start.weekday()) % 7 or 7)
        while d <= end:
            yield d
            d += timedelta(days=7)
        return
    y, m = start.year, start.month
    while True:
        if kind == "third_friday":
            d = third_friday(y, m)
        elif kind == "quarterly":
            d = third_friday(y, m) if m in (3, 6, 9, 12) else None
        elif kind == "month_end":
            d = last_weekday_of_month(y, m)
        else:
            raise ValueError(f"unknown expiration rule kind {kind!r}")
        if d is not None:
            if d > end:
                return
            if d > start:
                yield d
        m += 1
        if m > 12:
            y, m = y + 1, 1
        if date(y, m, 1) > end:
            return
