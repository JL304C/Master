"""Rule tests on hand-built bar sequences.

Box candle: high 110, low 100 -> H = 10. Long stop (stop_k=2) = 90, short stop = 120.
"""
import numpy as np
import pandas as pd
import pytest

from src.backtest import Instrument, plan_levels, run
from src.box import build_market, find_setup, sessions

BASE = {
    "box_open_time": "18:00", "box_tz": "America/New_York", "skip_pct": None, "skip_lookback": 50,
    "entry_mode": "snapback", "max_wait_bars": 6, "touch_depth": 0.0, "require_turn_candle": True,
    "allow_reentry_after_close_inside": False, "stop_k": 2.0, "be_trigger": 1.0, "be_buffer": 0.03,
    "trail_mode": "off", "trail_buffer": 0.0, "trail_step": 1.0, "trail_points": 1.0,
    "target_mode": "hold", "fixed_tp": 1, "partials": False, "rr_target": 2.0, "eod_exit": True,
}
NOCOST = Instrument("T", point_value=1.0, tick_size=0.25, commission_per_side=0.0, slippage_ticks=0)
ACCT = {"starting_equity": 100_000, "risk_per_trade": 0.01, "fractional_size": True}
BOX = (105, 110, 100, 106)


def p(**kw):
    return {**BASE, **kw}


def five_min(o, h, l, c):
    """12 five-minute bars whose combined OHLC is exactly (o, h, l, c)."""
    path = [o, l, h, c] if c >= o else [o, h, l, c]
    pts = np.concatenate([np.linspace(path[k], path[k + 1], 5)[(1 if k else 0):] for k in range(3)])
    pts = np.interp(np.linspace(0, len(pts) - 1, 13), np.arange(len(pts)), pts)
    rows = []
    for a, b in zip(pts[:-1], pts[1:]):
        rows.append((a, max(a, b), min(a, b), b))
    rows[0] = (o, *rows[0][1:])
    return rows


def market(hours, start="2024-06-03 18:00", explicit=None):
    """hours: list of hourly (o,h,l,c); explicit: {hour_index: [12 five-min rows]} overrides."""
    t0 = pd.Timestamp(start, tz="America/New_York")
    rows, idx = [], []
    for k, bar in enumerate(hours):
        sub = (explicit or {}).get(k) or five_min(*bar)
        for i, r in enumerate(sub):
            idx.append(t0 + pd.Timedelta(hours=k, minutes=5 * i))
            rows.append(r)
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=pd.DatetimeIndex(idx))
    df["volume"] = 1
    return build_market(df)


def setup_for(hours, **kw):
    m = market(hours)
    s = sessions(m, p(**kw))[0]
    return m, s, find_setup(m, s, p(**kw))


# ---- box ---------------------------------------------------------------------------------

def test_box_uses_wicks_and_dst():
    for start in ("2024-06-03 18:00", "2024-01-08 18:00"):  # EDT and EST
        m = market([BOX, (106, 108, 104, 107)], start=start)
        s = sessions(m, p())[0]
        assert (s.box_high, s.box_low) == (110, 100)
        assert m.t1[s.box_i].hour == 18


def test_tokyo_box_is_20_et_in_summer_19_in_winter():
    hours = [(100, 101, 99, 100)] * 4
    m = market(hours, start="2024-06-03 18:00")
    s = sessions(m, p(box_open_time="09:00", box_tz="Asia/Tokyo"))[0]
    assert m.t1[s.box_i].hour == 20
    m = market(hours, start="2024-01-08 18:00")
    s = sessions(m, p(box_open_time="09:00", box_tz="Asia/Tokyo"))[0]
    assert m.t1[s.box_i].hour == 19


# ---- break and snapback ------------------------------------------------------------------

def test_close_at_box_high_is_not_a_break():
    _, _, (st, why) = setup_for([BOX, (106, 111, 105, 110), (110, 111, 109, 110.5)])
    assert st is None and why == "no_snapback"  # bar 2 is the break, no snapback after it


def test_long_snapback_entry_at_close():
    _, s, (st, why) = setup_for([BOX, (106, 113, 105, 112), (112, 114, 108, 113)])
    assert why == "entry" and st.direction == 1
    assert st.break_i == s.box_i + 1 and st.entry_i == s.box_i + 2
    assert st.entry_raw == 113 and st.snap_low == 108


def test_snapback_must_dip_into_box():
    _, _, (st, _) = setup_for([BOX, (106, 113, 105, 112), (112, 114, 110.5, 113)])
    assert st is None
    # looser comparison variant: within 0.1*H above the box counts
    _, _, (st, _) = setup_for([BOX, (106, 113, 105, 112), (112, 114, 110.5, 113)], touch_depth=-0.1)
    assert st is not None


def test_touch_depth_quarter_box():
    hours = [BOX, (106, 113, 105, 112), (112, 114, 109, 113)]
    assert setup_for(hours, touch_depth=0.25)[2][0] is None  # needs low <= 107.5
    hours[2] = (112, 114, 107, 113)
    assert setup_for(hours, touch_depth=0.25)[2][0] is not None


def test_turn_candle_toggle():
    hours = [BOX, (106, 113, 105, 112), (113, 114, 108, 111)]  # closes outside but down candle
    assert setup_for(hours)[2][0] is None
    assert setup_for(hours, require_turn_candle=False)[2][0] is not None


def test_snapback_must_close_outside_box():
    _, _, (st, _) = setup_for([BOX, (106, 113, 105, 112), (109, 112, 108, 109.5)])
    assert st is None


def test_close_inside_invalidates_unless_reentry_allowed():
    hours = [BOX, (106, 113, 105, 112), (112, 112, 106, 107), (107, 113, 106, 112), (112, 114, 108, 113)]
    assert setup_for(hours)[2][0] is None
    _, s, (st, _) = setup_for(hours, allow_reentry_after_close_inside=True)
    assert st.entry_i == s.box_i + 4 and st.break_i == s.box_i + 3


def test_opposite_close_cancels_and_arms_short():
    hours = [BOX, (106, 113, 105, 112), (112, 112, 97, 98), (98, 101, 96, 97)]
    _, s, (st, _) = setup_for(hours)
    assert st.direction == -1 and st.entry_i == s.box_i + 3 and st.snap_high == 101


def test_max_wait_expires_setup():
    up = (113, 116, 112, 115)
    snap = (112, 116, 109, 115)
    hours = [BOX, (106, 113, 105, 112)] + [up] * 6 + [snap]
    assert setup_for(hours)[2][0] is None                 # snapback on bar 7 after the break
    assert setup_for(hours, max_wait_bars=7)[2][0] is not None


def test_breakout_comparison_enters_on_break_candle():
    _, s, (st, _) = setup_for([BOX, (106, 113, 105, 112)], entry_mode="breakout")
    assert st.entry_i == s.box_i + 1 and st.entry_raw == 112


# ---- exits -------------------------------------------------------------------------------

LONG_ENTRY = [BOX, (106, 113, 105, 112), (112, 114, 108, 113)]  # long at 113, stop 90


def trades(hours, explicit=None, inst=NOCOST, **kw):
    m = market(hours, explicit=explicit)
    t, _ = run(m, p(**kw), inst, ACCT)
    return t


def test_stop_level_and_targets():
    m = market(LONG_ENTRY)
    s = sessions(m, p())[0]
    st, _ = find_setup(m, s, p())
    stop, tg = plan_levels(st, p(target_mode="fixed", partials=True))
    assert stop == 90 and tg == [(118, 0.5), (128, 0.5)]  # snapback low 108 + 1H / + 2H
    stop, tg = plan_levels(st, p(target_mode="rr", rr_target=2))
    assert tg == [(113 + 2 * 23, 1.0)]


def test_stop_hit_first_when_both_touched_in_same_bar():
    wide = [(113, 120, 89, 100)] + [(100, 100, 100, 100)] * 11
    t = trades(LONG_ENTRY + [(113, 120, 89, 100)], explicit={3: wide}, target_mode="fixed")
    assert t.iloc[0].exit_reason == "stop" and t.iloc[0].R == pytest.approx(-1)


def test_gap_through_stop_fills_at_open():
    gap = [(85, 86, 84, 85)] * 12
    t = trades(LONG_ENTRY + [(85, 86, 84, 85)], explicit={3: gap})
    assert t.iloc[0].exit_reason == "stop_gap" and t.iloc[0].exit_price == 85


def test_target_hit():
    t = trades(LONG_ENTRY + [(113, 119, 112, 118)], target_mode="fixed")
    assert t.iloc[0].exit_reason == "target" and t.iloc[0].R == pytest.approx(5 / 23)


def test_partials():
    t = trades(LONG_ENTRY + [(113, 119, 112, 118), (118, 129, 117, 128)], target_mode="fixed", partials=True)
    assert t.iloc[0].R == pytest.approx((0.5 * 5 + 0.5 * 15) / 23)


def test_breakeven_after_one_box_close_in_profit():
    # hour 3 closes at 124 >= 113 + 10 -> stop to 113.3; hour 4 drops to 100
    t = trades(LONG_ENTRY + [(113, 125, 112, 124), (124, 124, 100, 101)])
    r = t.iloc[0]
    assert r.exit_reason == "stop" and r.exit_price == pytest.approx(113.3) and r.be_hit


def test_breakeven_needs_a_close_not_a_wick():
    t = trades(LONG_ENTRY + [(113, 125, 112, 114), (114, 115, 100, 101)])
    assert t.iloc[0].exit_reason == "session_end" and t.iloc[0].exit_price == 101


def test_session_end_exit_at_next_box():
    hours = LONG_ENTRY + [(113, 115, 111, 114)] * 21 + [(114, 116, 113, 115)]  # bar 24 = next box
    t = trades(hours)
    assert t.iloc[0].exit_reason == "session_end" and t.iloc[0].exit_price == 114
    assert t.iloc[0].exit_time == pd.Timestamp("2024-06-04 18:00", tz="America/New_York")


def test_short_mirror():
    hours = [BOX, (104, 105, 97, 98), (98, 102, 96, 97), (97, 121, 96, 120.5)]
    t = trades(hours)
    r = t.iloc[0]
    assert r.direction == "short" and r.stop == 120 and r.entry_price == 97
    assert r.exit_reason == "stop" and r.R == pytest.approx(-1)


def test_swing_trail_moves_to_pullback_low():
    hours = LONG_ENTRY + [
        (113, 125, 112, 124),   # BE -> 113.3
        (124, 124, 118, 119),   # pullback, low 118
        (119, 130, 119, 129),   # new high after pullback -> stop to 118
        (129, 129, 110, 111),   # stopped at 118
    ]
    t = trades(hours, trail_mode="swing")
    assert t.iloc[0].exit_price == pytest.approx(118)


def test_points_trail():
    hours = LONG_ENTRY + [(113, 125, 112, 124), (124, 130, 123, 129), (129, 129, 110, 111)]
    t = trades(hours, trail_mode="points", trail_points=1.0)
    assert t.iloc[0].exit_price == pytest.approx(120)  # best high 130 - 1H


def test_costs_and_slippage_reduce_R():
    inst = Instrument("T", point_value=1.0, tick_size=0.25, commission_per_side=1.0, slippage_ticks=1)
    t = trades(LONG_ENTRY + [(113, 119, 112, 118)], inst=inst, target_mode="fixed")
    entry = 113.25
    assert t.iloc[0].R == pytest.approx(((118 - 0.25 - entry) - 2) / (entry - 90))


def test_sizing_risks_one_percent():
    t = trades(LONG_ENTRY + [(113, 114, 89, 90)])
    assert t.iloc[0].pnl == pytest.approx(-1000)
