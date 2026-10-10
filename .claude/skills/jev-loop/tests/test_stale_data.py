"""The stale-data limit must see a frozen feed. The first published version
stamped every book read with the local clock, so data_age_s was always ~0
and the loop kept quoting on a price Alpaca had stopped updating."""

import time

from jevloop import loop
from jevloop.assets import resolve_symbol
from jevloop.limits import Limits
from jevloop.risk import check
from jevloop.state import InventoryState, build_snapshot


class _Feed:
    def __init__(self, book_t=None, quote_t=None):
        self.book_t, self.quote_t = book_t, quote_t

    def get_orderbook(self):
        book = {"b": [{"p": 100.0, "s": 1.0}], "a": [{"p": 100.2, "s": 1.0}]}
        if self.book_t:
            book["t"] = self.book_t
        return book

    def get_latest_quote(self):
        quote = {"bp": 30.0, "bs": 5, "ap": 30.02, "as": 5}
        if self.quote_t:
            quote["t"] = self.quote_t
        return quote


def _snap(data_ts, now):
    return build_snapshot(
        as_of=now,
        mid=100.1,
        microprice=100.1,
        spread_bps=20.0,
        bid_depth=[(100.0, 1.0)],
        ask_depth=[(100.2, 1.0)],
        trade_prices=[(now, 100.1)],
        trade_sides=[],
        inv=InventoryState(equity_usd=1000.0, high_water_mark_usd=1000.0),
        data_timestamp=data_ts,
    )


def test_crypto_book_reports_the_venue_timestamp_not_the_read_time():
    stamp = "2026-10-09T22:40:40.123456789Z"
    *_, ts = loop._read_top_of_book(_Feed(book_t=stamp), resolve_symbol("BTC/USD"))
    assert ts == loop._parse_ts(stamp)


def test_equity_quote_reports_the_venue_timestamp():
    stamp = "2026-10-09T15:00:00Z"
    bids, asks, ts = loop._read_top_of_book(_Feed(quote_t=stamp), resolve_symbol("AAPL"))
    assert bids == [(30.0, 5.0)] and asks == [(30.02, 5.0)]
    assert ts == loop._parse_ts(stamp)


def test_missing_or_garbled_timestamp_counts_as_stale():
    btc = resolve_symbol("BTC/USD")
    assert loop._read_top_of_book(_Feed(), btc)[2] == 0.0
    assert loop._read_top_of_book(_Feed(book_t="not a time"), btc)[2] == 0.0


def test_frozen_feed_is_vetoed_and_a_fresh_one_is_not():
    now = time.time()
    L = Limits()
    frozen = _snap(data_ts=now - 60.0, now=now)  # Alpaca stopped updating a minute ago
    assert frozen["data_age_s"] == 60.0
    v = check(frozen, 20.0, L, 0, 90.0)
    assert not v.ok and not v.kill and "stale" in v.veto

    fresh = _snap(data_ts=now - 1.0, now=now)
    assert check(fresh, 20.0, L, 0, 90.0).ok
