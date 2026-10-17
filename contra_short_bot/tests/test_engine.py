import numpy as np
import pandas as pd

from data.fetch import load_config
from engine.rejection import Focus, Gate, Setup
from engine.types import Fill, MoveStop, Place
from levels.model import Level
from risk.sizing import capped_add, full_size

CFG = load_config()
ET = "America/New_York"


def feats(bars, start="2024-05-08 10:30"):
    """bars: list of (open, high, low, close). Builds the columns Setup reads."""
    a = np.asarray(bars, float)
    idx = pd.date_range(start, periods=len(a), freq="1min", tz=ET)
    f = pd.DataFrame(a, columns=["open", "high", "low", "close"], index=idx)
    f["atr1"], f["atr5"] = 0.20, 0.40
    f["body"] = (f.close - f.open).abs()
    f["wide"] = f["body"] >= 1.5 * f["body"].expanding().mean().shift(1).fillna(1e9)
    f["red"] = f.close < f.open
    f["lower_half"] = (f.close - f.low) <= 0.5 * (f.high - f.low)
    hh = (f.high > f.high.shift(1)).astype(int)
    f["hh_run"] = hh.groupby((hh == 0).cumsum()).cumsum()
    f["hh_run_prev"] = f["hh_run"].shift(1).fillna(0)
    f["rise_n"] = f.high - f.low.rolling(15, min_periods=1).min()
    for c in ["hod_vol_1m", "hod_vol_5m", "vol_ramp", "quickstrike_high", "sp_top_reversal", "gap_reversal",
              "m2_higher_high"]:
        f[c] = False
    f["m2_below_ema9"] = True
    f["contra_short"], f["vdif"] = 0, 0.0
    return f


def run(setup, f, gate=Gate(True)):
    focus = Focus(CFG)
    acts = []
    for i, row in enumerate(f.itertuples()):
        a = setup.step(i, f, row, focus, gate, True)
        acts += [(i, x) for x in a]
    return acts


LEVEL = Level("pivot", 100.0, 100.4, 6, 3, labels=["pivot"])


def ramp(a, b, n):
    """n green bars stepping from a to b."""
    xs = np.linspace(a, b, n + 1)
    return [(x0, x1 + 0.02, x0 - 0.02, x1) for x0, x1 in zip(xs[:-1], xs[1:])]


def test_fast_rally_rejected_at_level_places_starter():
    bars = ramp(98.5, 99.4, 6) + ramp(99.4, 100.3, 3) + [(100.3, 100.5, 100.05, 100.10)]
    s = Setup("X", "A", LEVEL, CFG, 1_000_000)
    places = [x for _, x in run(s, feats(bars)) if isinstance(x, Place)]
    assert len(places) == 1
    o = places[0].order
    assert o.price == round(100.05 - 0.01, 2)            # rejection-bar low - 1 tick
    assert s.orig_stop == round(100.50 + max(0.5 * 0.20, 0.001 * 100.10), 2)   # Scenario B above the spike
    full = full_size(1_000_000, o.price, s.orig_stop, CFG)
    assert o.qty == full // 3                              # scaled: starter = 1/3


def test_slow_drift_into_level_not_traded():
    bars = ramp(99.7, 100.3, 30) + [(100.3, 100.35, 100.05, 100.10)]
    s = Setup("X", "A", LEVEL, CFG, 1_000_000)
    acts = run(s, feats(bars))
    assert not [x for _, x in acts if isinstance(x, Place)]
    assert any("slow approach" in getattr(x, "text", "") for _, x in acts)


def test_level_fails_after_too_many_closes_above():
    bars = ramp(98.5, 100.2, 4) + [(100.6, 100.8, 100.55, 100.7)] * 5 + [(100.7, 100.7, 100.0, 100.05)]
    s = Setup("X", "A", LEVEL, CFG, 1_000_000)
    acts = run(s, feats(bars))
    assert s.state == "failed"
    assert not [x for _, x in acts if isinstance(x, Place)]


def test_price_falling_from_above_is_not_a_test():
    bars = [(101.0, 101.1, 100.9, 101.0)] * 6 + [(101.0, 101.0, 100.2, 100.25), (100.25, 100.3, 99.9, 99.95)]
    s = Setup("X", "A", LEVEL, CFG, 1_000_000)
    acts = run(s, feats(bars))
    assert not [x for _, x in acts if isinstance(x, Place)]
    assert s.state == "idle"


def test_fill_sets_scenario_b_stop_and_add_moves_to_scenario_c():
    bars = ramp(98.5, 99.4, 6) + ramp(99.4, 100.3, 3) + [(100.3, 100.5, 100.05, 100.10)]
    # after the entry: a dip, a lower high at 100.30, then a lower close
    bars += [(100.10, 100.12, 99.95, 99.98), (99.98, 100.30, 99.97, 100.20), (100.20, 100.22, 100.00, 100.02),
             (100.02, 100.05, 99.90, 99.92)]
    f = feats(bars)
    s = Setup("X", "A", LEVEL, CFG, 1_000_000)
    focus, acts = Focus(CFG), []
    for i, row in enumerate(f.itertuples()):
        out = s.step(i, f, row, focus, Gate(True), True)
        for a in out:
            acts.append(a)
            if isinstance(a, Place):
                fill_i = i + 1
                acts += s.on_fill(Fill(a.order.oid, "X", "sell", a.order.qty, a.order.price, f.index[fill_i]), fill_i)
    stops = [a for a in acts if isinstance(a, MoveStop)]
    assert stops[0].price == s.orig_stop                   # Scenario B on the starter fill
    adds = [a.order for a in acts if isinstance(a, Place) and a.order.tag == "entry2"]
    assert adds and adds[0].price == round(99.97 - 0.01, 2)    # low of the lower-high bar - 1 tick
    assert s.stop == round(min(100.30 + 0.25 * 0.20, s.orig_stop), 2)   # Scenario C inside the spike
    assert stops[-1].price == s.stop


def test_capped_add_keeps_total_risk():
    # starter 100 sh @ 50.00, new stop 50.50, add at 49.80: budget $200
    q = capped_add(200.0, [(100, 50.0)], 50.5, 49.8, want=500)
    assert q == int((200 - 100 * 0.5) // 0.7)
    assert 100 * 0.5 + q * 0.7 <= 200
