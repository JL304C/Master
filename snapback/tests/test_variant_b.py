"""Variant B (US-open box, 5-minute breakout + pullback) on hand-built bars.

Box = 09:00-10:00 ET candle, high 110, low 100 -> H = 10. Long stop = box low (100).
"""
import pandas as pd
import pytest

from src.backtest import plan_levels, run
from src.box import find_setup_b, sessions
from tests.test_rules import ACCT, BASE, BOX, NOCOST, market

B = {**BASE, "variant": "B", "box_open_time": "09:00", "entry_mode": "pullback", "min_wick_ratio": 0.3,
     "cancel_on_close_inside": False, "b_window_hours": 2, "eod_time": "16:00", "stop_k": 1.0,
     "be_trigger": None, "trail_mode": "off", "target_mode": "fixed", "target_base": "entry", "fixed_tp": 2}
FLAT = (112.5, 112.6, 112.4, 112.5)


def pb(**kw):
    return {**B, **kw}


def day(first_hour_bars, extra_hours=(), start="2024-06-03 09:00"):
    """Box hour, then a 10:00 hour given as 5-min bars (padded with flat bars), then extra hourly bars."""
    bars = list(first_hour_bars) + [FLAT] * (12 - len(first_hour_bars))
    hours = [BOX, (bars[0][0], max(b[1] for b in bars), min(b[2] for b in bars), bars[-1][3])] + list(extra_hours)
    return market(hours, start=start, explicit={1: bars})


BREAK = (108, 112.2, 107.5, 112)
PULLBACK = (112, 113, 109.5, 112.5)      # wick 2.5 of range 3.5 = 0.71


def setup(m, **kw):
    s = sessions(m, pb(**kw))[0]
    return find_setup_b(m, s, pb(**kw))


def test_break_then_pullback_entry():
    m = day([BREAK, (112, 113, 111, 112.5), PULLBACK])
    st, why = setup(m)
    assert why == "entry" and st.direction == 1 and st.entry_raw == 112.5
    assert st.entry_time == pd.Timestamp("2024-06-03 10:15", tz="America/New_York")
    assert st.break_time == pd.Timestamp("2024-06-03 10:05", tz="America/New_York")


def test_no_pullback_no_trade():
    st, why = setup(day([BREAK, (112, 115, 111, 114.5)]))
    assert st is None and why == "no_pullback"


def test_wick_ratio_filter():
    weak = (111, 113, 109.9, 112.9)      # lower wick 1.1 / range 3.1 = 0.35
    assert setup(day([BREAK, weak]))[0] is not None
    assert setup(day([BREAK, weak]), min_wick_ratio=0.5)[0] is None
    assert setup(day([BREAK, (112, 113, 109.9, 110.5)]), min_wick_ratio=None)[0] is not None


def test_pullback_must_close_outside_box():
    assert setup(day([BREAK, (112, 112.5, 108, 109.5)]))[0] is None


def test_close_inside_only_cancels_when_toggled():
    bars = [BREAK, (112, 112, 108, 109), PULLBACK]
    assert setup(day(bars))[0] is not None
    assert setup(day(bars), cancel_on_close_inside=True)[0] is None


def test_opposite_close_arms_short():
    bars = [BREAK, (112, 112, 97, 98), (98, 100.5, 97.5, 98)]  # upper wick 2.5 / 3 = 0.83
    st, _ = setup(day(bars))
    assert st.direction == -1 and st.entry_raw == 98


def test_trading_window():
    quiet = (105, 106, 104, 105)
    late = [BREAK, PULLBACK] + [FLAT] * 10                    # break and pullback at 12:00-12:15
    hours = [BOX, quiet, quiet, (108, 113, 107.5, 112.5)]
    m = market(hours, start="2024-06-03 09:00", explicit={3: late})
    assert setup(m)[0] is None                                # outside the 10:00-12:00 window
    st, _ = setup(m, b_window_hours=4)
    assert st.entry_time == pd.Timestamp("2024-06-03 12:10", tz="America/New_York")


def test_stop_at_box_low_and_target_two_box_heights():
    m = day([BREAK, PULLBACK])
    st, _ = setup(m)
    stop, tg = plan_levels(st, pb())
    assert stop == 100 and tg == [(112.5 + 20, 1.0)]


def test_exit_at_16_et():
    m = day([BREAK, PULLBACK], extra_hours=[(112.5, 114, 111, 113)] * 7)  # 11:00 .. 17:00
    t, _ = run(m, pb(), NOCOST, ACCT)
    r = t.iloc[0]
    assert r.exit_reason == "session_end" and r.exit_time == pd.Timestamp("2024-06-03 16:00", tz="America/New_York")
    assert r.R == pytest.approx((113 - 112.5) / 12.5)


def test_target_hit_same_day():
    m = day([BREAK, PULLBACK], extra_hours=[(112.5, 133, 112, 132)])
    t, _ = run(m, pb(), NOCOST, ACCT)
    assert t.iloc[0].exit_reason == "target" and t.iloc[0].R == pytest.approx(20 / 12.5)


def test_breakout_comparison_enters_on_break_bar():
    st, _ = setup(day([BREAK, PULLBACK]), entry_mode="breakout")
    assert st.entry_raw == 112 and st.entry_time == pd.Timestamp("2024-06-03 10:05", tz="America/New_York")
