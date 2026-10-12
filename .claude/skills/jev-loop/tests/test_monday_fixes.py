"""Bugs from the first weekday run (2026-10-12): unsellable dust froze the
loop for the rest of the run, the sell leg was refused every tick because
our own resting ask had the coin set aside, and RUN/REDUCE flips re-placed
the quotes every few seconds."""

import time

from jevloop import loop
from jevloop.assets import resolve_symbol
from jevloop.limits import Limits
from jevloop.policy import QUOTE_WIDE, Action
from jevloop.risk import check
from jevloop.state import InventoryState, apply_fill

BTC = resolve_symbol("BTC/USD")


# -- 1. dust is flat ---------------------------------------------------------


def test_dust_after_a_close_is_treated_as_flat():
    inv = InventoryState()
    apply_fill(inv, "buy", 0.000485829, 82_211.0, time.time() - 1000)
    apply_fill(inv, "sell", 0.00048582, 82_658.66, time.time())  # 0.000000009 left
    assert inv.inventory > 0  # the bug: this counted as a live position
    assert loop._drop_dust(inv, BTC) is True
    assert inv.inventory == 0.0 and inv.position_opened_at is None


def test_a_real_position_is_not_dust():
    inv = InventoryState(inventory=0.00000002, position_opened_at=1.0)
    assert loop._drop_dust(inv, BTC) is False and inv.inventory == 0.00000002


def test_after_dropping_dust_the_age_limit_no_longer_vetoes():
    inv = InventoryState(inventory=0.000000009, position_opened_at=time.time() - 5000)
    loop._drop_dust(inv, BTC)
    snap = dict(drawdown_pct=0.0, inventory=inv.inventory, mid=82_000.0, daily_loss_usd=0.0,
                position_age_s=0.0, data_age_s=0.1)
    assert check(snap, 20.0, Limits(), 0, 90.0).ok


# -- 2. the sell leg frees the coin our own ask set aside --------------------


class _Broker:
    _order_seq = 0

    def __init__(self, held, free):
        self.held, self.free = held, free
        self.sent, self.cancels = [], 0

    def submit_limit_order(self, side, qty, px):
        self.sent.append(("limit", side, qty))
        return {"client_order_id": f"t-{len(self.sent)}"}

    def submit_market_order(self, side, qty):
        if qty > self.free + 1e-12:
            raise AssertionError("Alpaca would refuse this: insufficient balance")
        self.sent.append(("market", side, qty))
        return {"client_order_id": f"t-{len(self.sent)}"}

    def cancel_own_orders(self):
        self.cancels += 1
        self.free = self.held  # the cancel released what the ask set aside
        return 1

    def get_position_qty(self):
        return self.held

    def get_position_held_and_free(self):
        return self.held, self.free


def _sell_signal(broker, inv, resting, rest_counter=0):
    snap = dict(drawdown_pct=0.0, inventory=inv.inventory, mid=82_600.0,
                daily_loss_usd=0.0, position_age_s=10.0, data_age_s=0.1)
    return loop._execute_action(
        alpaca=broker, spec=BTC, action=Action(QUOTE_WIDE, "t", direction_leg="down"),
        bid_px=82_400.0, ask_px=82_800.0, mid=82_600.0, quote_notional=20.0,
        directional_notional=20.0, snapshot=snap, limits=Limits(), inv=inv,
        api_error_streak=0, decision_latency_ms=100.0, resting_quotes=resting,
        rest_counter=rest_counter, now=time.time(), expected_px={},
    )


def test_sell_leg_pulls_our_ask_then_sells_everything():
    held = 0.000485829
    b = _Broker(held=held, free=0.000244299)  # half is under our own resting ask
    inv = InventoryState(inventory=held)
    resting = loop._resting(82_400.0, 82_800.0, inv, 20.0, ("buy", "sell"), ("buy", "sell"))
    _, fill_txt, _, _, resting, _ = _sell_signal(b, inv, resting)
    assert b.cancels == 1 and resting is None
    assert ("market", "sell", 0.00048582) in b.sent  # all of it, floored to the step
    assert "closing long" in fill_txt


def test_sell_leg_waits_if_the_coin_is_not_released_yet():
    b = _Broker(held=0.0004, free=0.0002)
    b.cancel_own_orders = lambda: 1  # cancel accepted but not processed yet
    inv = InventoryState(inventory=0.0004)
    resting = loop._resting(1.0, 2.0, inv, 20.0, ("sell",), ("sell",))
    _, fill_txt, *_ = _sell_signal(b, inv, resting)
    assert not [s for s in b.sent if s[0] == "market"]
    assert "waiting" in fill_txt


def test_no_ask_is_posted_in_the_tick_that_sells_the_position():
    b = _Broker(held=0.0004, free=0.0004)
    inv = InventoryState(inventory=0.0004)
    _sell_signal(b, inv, resting=None)
    assert not [s for s in b.sent if s[:2] == ("limit", "sell")]
    assert [s for s in b.sent if s[:2] == ("market", "sell")]


# -- 3. RUN/REDUCE flips don't re-place quotes -------------------------------


def test_growing_back_after_reduce_does_not_requote():
    inv = InventoryState()
    resting = loop._resting(100.0, 101.0, inv, 10.0, ("buy",), ("buy",))  # placed on REDUCE
    assert loop._quotes_still_good(resting, 100.0, 101.0, inv, 20.0, Limits())  # back on RUN


def test_shrinking_still_requotes():
    inv = InventoryState()
    resting = loop._resting(100.0, 101.0, inv, 20.0, ("buy",), ("buy",))
    assert not loop._quotes_still_good(resting, 100.0, 101.0, inv, 10.0, Limits())
