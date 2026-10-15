"""Variant C (US-open range fade) on hand-built bars.

Box = 09:00-10:00 ET candle, high 110, low 100 -> H = 10.
Short fade: stop 110 + 2.5 = 112.5, target 100. Long fade: stop 97.5, target 110.
"""
import pandas as pd
import pytest

from src.backtest import plan_levels, run
from src.box import find_setup_c, sessions
from tests.test_rules import ACCT, BASE, BOX, NOCOST, market

C = {**BASE, "variant": "C", "box_open_time": "09:00", "entry_mode": "rejection", "touch_depth": 0.0,
     "min_wick_ratio": 0.3, "b_window_hours": 2, "eod_time": "16:00", "c_stop_buffer": 0.25,
     "c_target": "opposite", "be_trigger": None, "trail_mode": "off"}
MIDBAR = (105, 105.5, 104.5, 105)
REJECT_TOP = (108, 110.5, 107.5, 108)    # upper wick 2.5 / range 3 = 0.83, closes inside
REJECT_BOTTOM = (102, 102.5, 99.5, 102)  # lower wick 2.5 / range 3


def pc(**kw):
    return {**C, **kw}


def day(bars, extra_hours=()):
    bars = list(bars) + [MIDBAR] * (12 - len(bars))
    hour = (bars[0][0], max(b[1] for b in bars), min(b[2] for b in bars), bars[-1][3])
    return market([BOX, hour] + list(extra_hours), start="2024-06-03 09:00", explicit={1: bars})


def setup(m, **kw):
    s = sessions(m, pc(**kw))[0]
    return find_setup_c(m, s, pc(**kw))


def test_short_fade_at_box_high():
    st, why = setup(day([MIDBAR, REJECT_TOP]))
    assert why == "entry" and st.direction == -1 and st.entry_raw == 108
    assert st.entry_time == pd.Timestamp("2024-06-03 10:10", tz="America/New_York")
    stop, tg = plan_levels(st, pc())
    assert stop == 112.5 and tg == [(100, 1.0)]
    assert plan_levels(st, pc(c_target="mid"))[1] == [(105, 1.0)]


def test_long_fade_at_box_low():
    st, _ = setup(day([REJECT_BOTTOM]))
    assert st.direction == 1
    stop, tg = plan_levels(st, pc())
    assert stop == 97.5 and tg == [(110, 1.0)]


def test_close_outside_ends_fading_for_the_day():
    st, why = setup(day([(108, 111, 108, 110.5), REJECT_TOP]))
    assert st is None and why == "range_broken"


def test_wick_filter():
    weak = (108.5, 110.2, 108, 109.8)   # upper wick 0.4 / range 2.2 = 0.18
    assert setup(day([weak]))[0] is None
    assert setup(day([weak]), min_wick_ratio=None)[0] is not None
    assert setup(day([weak]), entry_mode="breakout")[0] is not None


def test_must_touch_the_edge():
    near = (108, 109.5, 107.5, 108)
    assert setup(day([near]))[0] is None
    assert setup(day([near]), touch_depth=-0.1)[0] is not None   # within 0.1*H counts


def test_trade_hits_target_and_stop():
    t, _ = run(day([REJECT_TOP], extra_hours=[(108, 108.5, 99.5, 100.5)]), pc(), NOCOST, ACCT)
    assert t.iloc[0].exit_reason == "target" and t.iloc[0].R == pytest.approx(8 / 4.5)
    t, _ = run(day([REJECT_TOP], extra_hours=[(108, 113, 107, 112.8)]), pc(), NOCOST, ACCT)
    assert t.iloc[0].exit_reason == "stop" and t.iloc[0].R == pytest.approx(-1)
