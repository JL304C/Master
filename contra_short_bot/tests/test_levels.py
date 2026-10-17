import numpy as np
import pandas as pd
import pytest

from data.bars import adjust_daily, resample
from data.fetch import load_config
from indicators.core import atr
from levels.library import build_levels_from_daily
from levels.model import Level, merge_levels
from levels.pivots import find_pivots, swing_points
from levels.structure import find_gaps, find_swings
from levels.trendlines import find_trendlines, zigzag

CFG = load_config()


def bars(closes, spread=1.0, start="2024-01-02"):
    c = np.asarray(closes, float)
    idx = pd.bdate_range(start, periods=len(c))
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) + spread, "low": np.minimum(o, c) - spread,
                         "close": c, "volume": 1_000_000}, index=idx)


def wave(n_cycles=6, top=100.0, bottom=80.0, half=10):
    """Price oscillating between bottom and top: repeated tests of `top`."""
    up = np.linspace(bottom, top, half)
    down = np.linspace(top, bottom, half)
    return np.concatenate([np.r_[up, down[1:-1]] for _ in range(n_cycles)])


def test_swing_points_find_peaks_without_lookahead():
    df = bars(wave(3))
    sp = swing_points(df, 3)
    highs = sp[sp.src == "swing_high"]
    assert len(highs) >= 2
    assert (highs.price > 100).all()
    # The last 3 bars are never swings.
    assert sp.date.max() <= df.index[-4]


def test_pivot_at_repeated_tops():
    df = bars(wave(6))
    pv = find_pivots(df, atr(df), CFG)
    tops = [p for p in pv if p.zone_low <= 101 <= p.zone_high]
    assert tops, [p.label() for p in pv]
    assert tops[0].touches >= 3


def test_no_pivot_from_two_touches():
    df = bars(np.r_[np.linspace(80, 100, 15), np.linspace(100, 60, 30), np.linspace(60, 100, 30), np.linspace(100, 70, 20)])
    pv = find_pivots(df, atr(df), CFG)
    assert not [p for p in pv if p.zone_low <= 101 <= p.zone_high]


def test_unfilled_down_gap_makes_gap_fill_level():
    c = np.r_[np.full(40, 100.0), np.full(20, 85.0)]
    df = bars(c, spread=0.5)
    df.loc[df.index[40], ["open", "high", "low", "close"]] = [86, 87, 84.5, 85]
    gaps = find_gaps(df, atr(df), CFG)
    fill = [g for g in gaps if g.kind == "gap_fill"]
    assert len(fill) == 1 and abs(fill[0].mid - 99.5) < 1e-9
    entry = [g for g in gaps if g.kind == "gap_entry"]
    assert len(entry) == 1 and abs(entry[0].mid - 87) < 1e-9


def test_filled_gap_is_removed():
    c = np.r_[np.full(40, 100.0), np.full(10, 85.0), np.full(10, 101.0)]
    df = bars(c, spread=0.5)
    df.loc[df.index[40], ["open", "high", "low", "close"]] = [86, 87, 84.5, 85]
    assert not [g for g in find_gaps(df, atr(df), CFG) if g.kind == "gap_fill"]


def test_falling_trendline_through_lower_highs():
    # Three rallies to falling peaks 130, 120, then a touch near the line around 110.
    seg = lambda a, b, n: np.linspace(a, b, n)
    c = np.r_[seg(100, 130, 20), seg(130, 90, 20), seg(90, 120, 20), seg(120, 85, 20),
              seg(85, 110, 20), seg(110, 80, 20), seg(80, 90, 10)]
    df = bars(c)
    a = atr(df)
    sw = zigzag(df, a, CFG["levels"]["trendline"]["zigzag_atr"])
    assert [s[2] for s in sw][:3] == ["H", "L", "H"]
    tl = find_trendlines(df, a, CFG)
    assert any("falling resistance" in t.notes for t in tl), [t.notes for t in tl]


def test_merge_sma_on_pivot_gets_bonus():
    p = Level("pivot", 99, 101, 5, 3, labels=["pivot"])
    s = Level("sma", 100.5, 101.5, 2, labels=["200SMA"])
    far = Level("psych", 120, 121, 1, labels=["psych"])
    m = merge_levels([p, s, far], atr_d=3.0)
    assert len(m) == 2
    assert m[0].kind == "pivot" and m[0].strength == 5 + 2 + 2
    assert m[0].labels == ["pivot", "200SMA"]


def test_split_adjust_uses_only_known_splits(monkeypatch):
    import data.bars as b
    monkeypatch.setattr(b, "splits_for", lambda s: [(pd.Timestamp("2024-01-10"), 10.0)])
    df = bars(np.r_[np.full(6, 1000.0), np.full(6, 100.0)])
    before = adjust_daily(df, "X", pd.Timestamp("2024-01-09"))
    assert before["close"].iloc[0] == 1000
    after = adjust_daily(df, "X", pd.Timestamp("2024-01-10"))
    assert after["close"].iloc[0] == 100 and after["volume"].iloc[0] == 10_000_000


def test_levels_ignore_future_bars():
    rng = np.random.default_rng(0)
    c = 100 + np.cumsum(rng.normal(0, 1.5, 400))
    df = bars(c)
    day = df.index[300]
    a = build_levels_from_daily(df, "X", day, CFG, manual_path="/nonexistent")
    df2 = df.copy()
    df2.iloc[300:, :4] *= 3  # change everything from `day` on
    b = build_levels_from_daily(df2, "X", day, CFG, manual_path="/nonexistent")
    assert [l.label() for l in a.levels] == [l.label() for l in b.levels]


def test_resample_aligns_to_open():
    idx = pd.date_range("2024-01-02 09:30", periods=10, freq="1min", tz="America/New_York")
    df = pd.DataFrame({"open": range(10), "high": range(1, 11), "low": range(10), "close": range(10), "volume": 1},
                      index=idx, dtype=float)
    r = resample(df, 5)
    assert list(r.index.strftime("%H:%M")) == ["09:30", "09:35"]
    assert r["high"].iloc[0] == 5 and r["volume"].iloc[1] == 5


def test_double_top_becomes_a_swing_level():
    seg = lambda a, b, n: np.linspace(a, b, n)
    c = np.r_[seg(100, 150, 25), seg(150, 110, 25), seg(110, 150.5, 25), seg(150.5, 105, 25), seg(105, 115, 10)]
    df = bars(c)
    sw = find_swings(df, atr(df), CFG)
    tops = [s for s in sw if "double top" in s.notes]
    assert len(tops) == 1 and tops[0].zone_low < 151 < tops[0].zone_high and tops[0].strength == 4
