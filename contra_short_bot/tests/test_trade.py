import numpy as np
import pandas as pd

from data.fetch import load_config
from engine.types import Order
from execution.sim_broker import Pos, SimBroker
from levels.library import DayLevels
from levels.model import Level
from risk.limits import RiskBook
from trade.manager import TradeManager

CFG = load_config()
ET = "America/New_York"


class Bar:
    def __init__(self, o, h, l, c):
        self.open, self.high, self.low, self.close = o, h, l, c


def frame(n=30, px=100.0, vwap=99.0):
    idx = pd.date_range("2024-05-08 10:00", periods=n, freq="1min", tz=ET)
    f = pd.DataFrame({"open": px, "high": px + 0.1, "low": px - 0.1, "close": px, "vwap": vwap, "atr5": 0.4,
                      "m5_low": np.nan, "m5_high": np.nan}, index=idx)
    return f


def levels(*supports):
    lv = [Level("pivot", s - 0.05, s, 5, labels=["pivot"]) for s in supports]
    return DayLevels("X", pd.Timestamp("2024-05-08"), 100.0, 2.0, lv, lv)


def test_structure_targets_pick_first_support_beyond_each_r():
    tm = TradeManager("X", "A", CFG, entry=100.0, first_stop=100.5)       # 1R = 0.50
    tm.build(29, frame(vwap=99.8), levels(99.4, 98.9, 98.4))
    # VWAP 99.8 is only 0.4R away -> skipped; 99.4 = 1.2R -> T1, 98.9 = 2.2R -> T2, 98.4 = 3.2R -> T3
    assert [t.level for t in tm.targets] == [99.4, 98.9, 98.4]
    inside = max(CFG["trade"]["inside_min"], CFG["trade"]["inside_atr5"] * 0.4)
    assert tm.targets[0].limit == round(99.4 + inside, 2)                  # several ticks above support


def test_hybrid_minimum_uses_2r_when_no_support_far_enough():
    tm = TradeManager("X", "A", CFG, entry=100.0, first_stop=100.5)
    tm.build(29, frame(vwap=99.6), levels(99.7))                          # nothing >= 1.5R below
    assert tm.targets[0].label == "2R" and tm.targets[0].level == 99.0


def test_strategy_d_has_no_risk_multiple_fallback():
    tm = TradeManager("X", "D", CFG, entry=100.0, first_stop=100.5)
    tm.build(29, frame(vwap=101), levels())
    assert tm.targets == []


def test_thirds_and_breakeven():
    tm = TradeManager("X", "A", CFG, entry=100.0, first_stop=100.5)
    tm.build(29, frame(vwap=101), levels(99.4, 98.9, 98.4))
    assert tm.qty_for(0, 300) == 100
    assert tm.on_target(0) == 100.0                 # stop to breakeven after T1
    assert tm.qty_for(1, 200) == 100
    tm.on_target(1)
    assert tm.open_targets() == []                  # the last third trails


def test_stop_fills_before_target_in_same_bar():
    b = SimBroker(CFG)
    tm = TradeManager("X", "A", CFG, entry=100.0, first_stop=100.5)
    tm.build(29, frame(vwap=101), levels(99.4))
    p = Pos("X", "A", pd.Timestamp("2024-05-08 10:00", tz=ET), fills=[(None, 300, 100.0, "entry1")],
            stop=100.5, first_stop=100.5, tm=tm)
    b.pos["X"] = p
    ex = b.exits("X", None, Bar(100.0, 100.6, 99.0, 99.5))   # both the stop and T1 inside the bar
    assert ex[0][2] == "stop" and ex[0][0] == 300
    assert abs(ex[0][1] - 100.5 * (1 + 3 / 1e4)) < 1e-4      # +3 bps slippage (rounded to 4 dp)


def test_limit_needs_trade_through_and_entry_gap_fills_at_open():
    b = SimBroker(CFG)
    tm = TradeManager("X", "A", CFG, entry=100.0, first_stop=100.5)
    tm.build(29, frame(vwap=101), levels(99.4))
    lim = tm.targets[0].limit
    b.pos["X"] = Pos("X", "A", None, fills=[(None, 300, 100.0, "entry1")], stop=100.5, first_stop=100.5, tm=tm)
    assert b.exits("X", None, Bar(99.8, 99.9, lim, 99.8)) == []              # touched, not through
    assert b.exits("X", None, Bar(99.8, 99.9, lim - 0.01, 99.8))[0][2] == "T1"
    o = Order("Y", "sell", "stop", 100, 50.0, 3, "A", "entry1", "")
    b.orders[o.oid] = o
    (_, px), = b.entries("Y", Bar(49.5, 49.6, 49.0, 49.2))                   # gapped through the trigger
    assert abs(px - 49.5 * (1 - 2 / 1e4)) < 1e-4


def test_risk_limits():
    rb = RiskBook(CFG, 1_000_000)
    assert rb.allow_entry("X", False, {"A", "B"})[0]
    assert not rb.allow_entry("X", False, {"A", "B", "C"})[0]            # max 3 open
    assert rb.allow_entry("A", True, {"A", "B", "C"})[0]                 # adding to an open position is fine
    rb.day_realized = -15_000
    ok, why = rb.allow_entry("X", False, set())
    assert not ok and "daily loss" in why
