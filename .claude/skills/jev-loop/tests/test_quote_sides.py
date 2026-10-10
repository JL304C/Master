"""Two-sided quoting on a long-or-flat account. The first published version
sent a full-size ask while flat; Alpaca refused it ("insufficient balance
for BTC") on every tick, and the refusal orphaned the bid placed just
before it, so bids piled up until the position cap stopped them."""

import time

from jevloop import loop
from jevloop.assets import resolve_symbol
from jevloop.execution.alpaca import AlpacaAPIError
from jevloop.limits import Limits
from jevloop.policy import QUOTE_WIDE, Action
from jevloop.state import InventoryState


class _Broker:
    _order_seq = 0

    def __init__(self, held=0.0, refuse_sells=False):
        self.held, self.refuse_sells = held, refuse_sells
        self.sent, self.cancels = [], 0

    def submit_limit_order(self, side, qty, px):
        if side == "sell" and self.refuse_sells:
            raise AlpacaAPIError(403, "insufficient balance for BTC")
        self._order_seq += 1
        self.sent.append((side, qty))
        return {"client_order_id": f"t-{self._order_seq}"}

    def cancel_own_orders(self):
        self.cancels += 1
        return 0

    def get_position_qty(self):
        return self.held


def _quote(broker, inv, resting=None, rest_counter=0):
    snap = dict(drawdown_pct=0.0, inventory=inv.inventory, mid=100_000.0,
                daily_loss_usd=0.0, position_age_s=0.0, data_age_s=0.1)
    return loop._execute_action(
        alpaca=broker,
        spec=resolve_symbol("BTC/USD"),
        action=Action(QUOTE_WIDE, "test"),
        bid_px=99_990.0,
        ask_px=100_010.0,
        mid=100_000.0,
        quote_notional=20.0,
        directional_notional=20.0,
        snapshot=snap,
        limits=Limits(),
        inv=inv,
        api_error_streak=0,
        decision_latency_ms=100.0,
        resting_quotes=resting,
        rest_counter=rest_counter,
        now=time.time(),
        expected_px={},
    )


def test_flat_account_quotes_the_bid_only():
    b = _Broker()
    _, fill_txt, _, _, resting, _ = _quote(b, InventoryState())
    assert [s for s, _ in b.sent] == ["buy"]
    assert "ask skipped" in fill_txt and resting is not None


def test_ask_never_offers_more_than_held():
    b = _Broker(held=0.0001)  # $10 held, quote target is $20
    inv = InventoryState(inventory=0.0001)
    _quote(b, inv)
    sells = [q for s, q in b.sent if s == "sell"]
    assert sells == [0.0001]


def test_ask_sized_by_what_alpaca_holds_not_the_bots_count():
    b = _Broker(held=0.00005)  # $5 really held: under the $10 minimum
    inv = InventoryState(inventory=0.0002)
    _, fill_txt, *_ = _quote(b, inv)
    assert not [s for s, _ in b.sent if s == "sell"]
    assert "ask skipped" in fill_txt


def test_a_refused_ask_does_not_orphan_the_bid():
    b = _Broker(held=0.0002, refuse_sells=True)
    inv = InventoryState(inventory=0.0002)
    line, _, _, _, resting, rest_counter = _quote(b, inv)
    assert "order error" in line
    assert ("buy", b.sent[0][1]) == b.sent[0] and resting is not None  # bid is tracked
    # next cycle cancel-replaces it rather than stacking a second bid on top
    b.refuse_sells = False
    _quote(b, inv, resting=resting, rest_counter=Limits().rest_ticks)
    assert b.cancels == 1


# -- only re-quote when the orders would actually change --------------------


def _quote_at(broker, inv, bid, resting=None, notional=20.0):
    snap = dict(drawdown_pct=0.0, inventory=inv.inventory, mid=100_000.0,
                daily_loss_usd=0.0, position_age_s=0.0, data_age_s=0.1)
    return loop._execute_action(
        alpaca=broker, spec=resolve_symbol("BTC/USD"), action=Action(QUOTE_WIDE, "test"),
        bid_px=bid, ask_px=bid + 20.0, mid=100_000.0, quote_notional=notional,
        directional_notional=20.0, snapshot=snap, limits=Limits(), inv=inv,
        api_error_streak=0, decision_latency_ms=100.0, resting_quotes=resting,
        rest_counter=Limits().rest_ticks, now=time.time(), expected_px={},
    )


def test_unchanged_price_leaves_the_working_order_alone():
    b, inv = _Broker(), InventoryState()
    *_, resting, _ = _quote_at(b, inv, 99_990.0)
    for _ in range(5):  # rest period after rest period, same price
        *_, resting, rc = _quote_at(b, inv, 99_990.0, resting=resting)
        assert rc == 0
    assert len(b.sent) == 1 and b.cancels == 0


def test_tiny_price_change_is_not_worth_a_requote():
    b, inv = _Broker(), InventoryState()
    *_, resting, _ = _quote_at(b, inv, 99_990.0)
    _quote_at(b, inv, 99_995.0, resting=resting)  # 0.5 bps
    assert len(b.sent) == 1 and b.cancels == 0


def test_real_price_move_requotes():
    b, inv = _Broker(), InventoryState()
    *_, resting, _ = _quote_at(b, inv, 99_990.0)
    _quote_at(b, inv, 99_900.0, resting=resting)  # 9 bps
    assert len(b.sent) == 2 and b.cancels == 1


def test_a_fill_requotes_even_at_the_same_price():
    b, inv = _Broker(held=0.0002), InventoryState()
    *_, resting, _ = _quote_at(b, inv, 99_990.0)
    inv.inventory = 0.0002  # the bid filled; reconcile updated inventory
    _quote_at(b, inv, 99_990.0, resting=resting)
    assert b.cancels == 1 and ("sell" in [s for s, _ in b.sent])  # now offers to sell it


def test_size_change_requotes():
    b, inv = _Broker(), InventoryState()
    *_, resting, _ = _quote_at(b, inv, 99_990.0, notional=20.0)
    _quote_at(b, inv, 99_990.0, resting=resting, notional=10.0)  # REDUCE rung
    assert b.cancels == 1
