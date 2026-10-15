"""Variant D (US-open breakout, no pullback): long on a 5-min close above the box, short below,
or a stop order resting one tick beyond each edge. Box 100-110, tick 0.25."""
import pandas as pd
import pytest

from src.backtest import run
from src.box import find_setup_b, sessions
from tests.test_rules import ACCT, NOCOST
from tests.test_variant_b import B, BREAK, FLAT, day

D = {**B, "variant": "D", "entry_mode": "breakout", "tick_size": 0.25}


def pd_(**kw):
    return {**D, **kw}


def setup(m, **kw):
    s = sessions(m, pd_(**kw))[0]
    return find_setup_b(m, s, pd_(**kw))


def test_long_on_close_above_box():
    st, _ = setup(day([(105, 106, 104, 105), BREAK]))
    assert st.direction == 1 and st.entry_raw == 112
    assert st.entry_time == pd.Timestamp("2024-06-03 10:10", tz="America/New_York")


def test_short_on_close_below_box():
    st, _ = setup(day([(101, 101.5, 98, 98.5)]))
    assert st.direction == -1 and st.entry_raw == 98.5


def test_stop_order_fills_at_edge_plus_tick():
    st, _ = setup(day([(108, 110.3, 107.5, 109.5)]), entry_mode="stop")   # pokes through, closes inside
    assert st.direction == 1 and st.entry_raw == 110.25
    inside = [(108, 110.3, 107.5, 109.5)] + [(105, 105.5, 104.5, 105)] * 11
    assert setup(day(inside))[0] is None                                    # close-based: no trade


def test_stop_order_gap_fills_at_open():
    st, _ = setup(day([(111, 112, 110.8, 111.5)]), entry_mode="stop")
    assert st.entry_raw == 111


def test_both_edges_in_one_bar_skips_day():
    st, why = setup(day([(105, 111, 99, 105)]), entry_mode="stop")
    assert st is None and why == "both_edges_one_bar"


def test_stop_order_trade_runs_to_target():
    m = day([(108, 110.3, 107.5, 109.5)], extra_hours=[(110, 131, 109, 130)])
    t, _ = run(m, pd_(entry_mode="stop"), NOCOST, ACCT)
    r = t.iloc[0]
    assert r.exit_reason == "target" and r.R == pytest.approx(20 / 10.25)


def test_close_entry_trade_stopped_at_box_low():
    m = day([BREAK], extra_hours=[(112, 112.5, 99, 99.5)])
    t, _ = run(m, pd_(), NOCOST, ACCT)
    assert t.iloc[0].exit_reason == "stop" and t.iloc[0].R == pytest.approx(-1)
    _ = FLAT
