import numpy as np
import pandas as pd
import pytest

import selection.scanners as sc
from data.fetch import load_config
from selection.context import DailyContext
from selection.market_tone import _combine, label_for

CFG = load_config()
ET = "America/New_York"


def session(day: str, closes, vols) -> pd.DataFrame:
    idx = pd.date_range(f"{day} 09:30", periods=len(closes), freq="1min", tz=ET)
    c = np.asarray(closes, float)
    o = np.r_[c[0], c[:-1]]
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) + 0.02, "low": np.minimum(o, c) - 0.02,
                         "close": c, "volume": np.asarray(vols, float)}, index=idx)


def fake_market(monkeypatch, today: pd.DataFrame, prior: list[pd.DataFrame], prev_close=100.0):
    window = pd.concat(prior + [today])
    day = today.index[0].normalize().tz_localize(None)
    hist = pd.DataFrame({"open": prev_close, "high": prev_close + 1, "low": prev_close - 1, "close": prev_close,
                         "volume": 1e6}, index=pd.bdate_range(end=day - pd.Timedelta(days=1), periods=40))
    ctx = DailyContext("X", day, prev_close, prev_close + 1, prev_close - 1, 2.0, hist)
    monkeypatch.setattr(sc, "rth_window", lambda s, d, n: window)
    monkeypatch.setattr(sc, "daily_context", lambda s, d: ctx)
    monkeypatch.setattr(sc, "has_history", lambda s, d: True)
    monkeypatch.setattr(sc, "day_minutes", lambda s, d, a=None, b=None: today.iloc[:1])
    return day


def flat_day(day, n=390, px=100.0, vol=1000):
    rng = np.random.default_rng(abs(hash(day)) % 2**32)
    return session(day, px + np.cumsum(rng.normal(0, 0.03, n)), np.full(n, vol))


def test_contra_short_fires_on_spike_above_vwap(monkeypatch):
    prior = [flat_day("2024-05-06"), flat_day("2024-05-07")]
    calm = 100 + np.zeros(60)
    spike = np.linspace(100, 106, 25)
    fade = np.linspace(106, 102, 60)
    closes = np.r_[calm, spike, fade]
    vols = np.r_[np.full(60, 3000), np.full(25, 9000), np.full(60, 3000)]
    day = fake_market(monkeypatch, session("2024-05-08", closes, vols), prior)
    df = sc.intraday_scan("X", day, CFG)
    hits = df[df["contra_short"] > 0]
    assert len(hits), "spike 6% above VWAP on 3x volume should print Contra Short"
    first = hits.index[0]
    assert first.strftime("%H:%M") > "10:30"           # only during the spike
    assert hits["new_day_high"].all()                     # always on a new high of day
    assert (df.loc[df.index > df["high"].idxmax(), "contra_short"] == 0).all()   # never on the fade


def test_no_contra_on_quiet_volume(monkeypatch):
    prior = [flat_day("2024-05-06", vol=3000), flat_day("2024-05-07", vol=3000)]
    closes = np.r_[np.full(60, 100.0), np.linspace(100, 106, 25)]
    day = fake_market(monkeypatch, session("2024-05-08", closes, np.full(85, 1000)), prior)
    df = sc.intraday_scan("X", day, CFG)
    assert (df["contra_short"] == 0).all()               # RVOL ~0.3 < 1.5


def test_five_minute_values_only_after_bar_completes():
    idx5 = pd.date_range("2024-05-08 09:30", periods=3, freq="5min", tz=ET)
    s5 = pd.Series([1.0, 2.0, 3.0], index=idx5)
    idx1 = pd.date_range("2024-05-08 09:30", periods=15, freq="1min", tz=ET)
    a = sc.at_completion(s5, idx1)
    assert a.iloc[:4].isna().all()                       # 09:30-09:33: first 5m bar still open
    assert a.iloc[4] == 1.0                              # the 09:34 bar closes at 09:35 with it
    assert a.iloc[9] == 2.0                              # 09:39


def test_tier():
    t = sc._tier(pd.Series([0.5, 1.2, 2.5, 3.1]), [1, 2, 3])
    assert list(t) == [0, 1, 2, 3]


def test_tone_labels_and_divergence():
    assert label_for(-0.9, CFG) == "WEAK" and label_for(0.0, CFG) == "NEUTRAL" and label_for(0.9, CFG) == "STRONG"
    w = CFG["data"]["index_proxies"]
    weak = {s: {"score": -2.0} for s in w}
    assert _combine(weak, w, CFG).label == "WEAK"
    mixed = {"SPY": {"score": 1.0}, "QQQ": {"score": 1.0}, "IWM": {"score": -1.0}, "DIA": {"score": 1.0}}
    t = _combine(mixed, w, CFG)
    assert t.divergence and t.label == "NEUTRAL"
