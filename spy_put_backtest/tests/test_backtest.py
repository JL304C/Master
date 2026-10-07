import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import backtest as bt  # noqa: E402
import synthetic  # noqa: E402
from data_sources import DatabentoSource, LocalChainSource, osi  # noqa: E402
from pricing import black76_put, fit_forward, implied_vol_put, put_delta, regt_naked_put_bp  # noqa: E402


def test_regt_formula():
    # OTM put: 20% * 500 - 50 OTM + 3 = 53 vs 10% * 450 + 3 = 48 -> 53
    assert regt_naked_put_bp(500, 450, 3.0) == pytest.approx(5300)
    # far OTM: 20%*500 - 150 + 1 < 0 -> 10% * 350 + 1 = 36
    assert regt_naked_put_bp(500, 350, 1.0) == pytest.approx(3600)


def test_iv_roundtrip_and_delta():
    F, T, DF = 400.0, 90 / 365, 0.99
    K = np.array([300.0, 350.0, 380.0, 400.0])
    sig = np.array([0.35, 0.25, 0.2, 0.17])
    px = black76_put(F, K, T, DF, sig)
    assert np.allclose(implied_vol_put(px, F, K, T, DF), sig, atol=1e-6)
    d = put_delta(F, K, T, sig)
    assert np.all((d < 0) & (d > -1)) and np.all(np.diff(d) < 0)


def test_parity_fit_recovers_rates():
    S, T = 300.0, 90 / 365
    K = np.arange(270.0, 331.0, 5.0)
    c = np.array([synthetic.bs(S, k, T, 0.2, "C") for k in K])
    p = np.array([synthetic.bs(S, k, T, 0.2, "P") for k in K])
    F, DF = fit_forward(K, c, p, ref_price=S)
    assert DF == pytest.approx(math.exp(-synthetic.R * T), abs=1e-6)
    assert F == pytest.approx(S * math.exp((synthetic.R - synthetic.Q) * T), abs=1e-4)


def _american_chain(d, S, r=0.043, q=0.012, seed=1):
    """Chain shaped like real SPY quotes: American floor on ITM options, widening spreads."""
    import datetime as dt
    rng = np.random.default_rng(seed)
    rows = []
    for dte in (1, 2, 4, 9, 30, 88, 95):
        e = d + dt.timedelta(days=dte)
        T = dte / 365
        F, DF = S * math.exp((r - q) * T), math.exp(-r * T)
        for K in np.arange(round(S * 0.9), round(S * 1.1) + 1, 1.0):
            sig = 0.16 * (1 - 1.5 * math.log(K / S))
            p = float(black76_put(F, K, T, DF, sig))
            c = p + DF * (F - K)
            p, c = max(p, K - S), max(c, S - K)          # American: never below intrinsic
            for right, v in (("P", p), ("C", c)):
                half = 0.01 + 0.004 * v
                mid = v + rng.normal(0, half / 3)
                if mid - half > 0:
                    rows.append((e, right, float(K), round(mid - half, 2), round(mid + half, 2)))
    return pd.DataFrame(rows, columns=["expiration", "right", "strike", "bid", "ask"])


def test_spot_and_forward_with_american_quotes():
    """Real SPY chains broke the old parity fit (deep ITM puts at intrinsic -> fitted DF > 1)."""
    import datetime as dt
    from data_sources import forward_for_expiry, spot_from_chain
    d, S = dt.date(2025, 4, 14), 539.12
    for seed in range(5):
        ch = _american_chain(d, S, seed=seed)
        assert spot_from_chain(ch, d) == pytest.approx(S, rel=0.0015)
        exp = d + dt.timedelta(days=88)
        F, DF, note = forward_for_expiry(ch, exp, S)
        assert F == pytest.approx(S * math.exp(0.031 * 88 / 365), rel=0.003)
        assert 0.98 < DF <= 1.0


def test_entry_days_shift_for_monday_holidays():
    import exchange_calendars as xcals
    s = [x.date() for x in xcals.get_calendar("XNYS").sessions_in_range("2024-01-01", "2024-01-31")]
    days = sorted(bt.entry_days(s))
    # Jan 1 and Jan 15 2024 were Monday holidays -> Tuesdays
    assert [str(d) for d in days] == ["2024-01-02", "2024-01-08", "2024-01-16", "2024-01-22", "2024-01-29"]


@pytest.fixture(scope="module")
def synth_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("chains")
    synthetic.generate(d)
    return d


@pytest.mark.parametrize("mode", ["mid", "conservative"])
def test_engine_end_to_end(synth_dir, mode, tmp_path):
    p = bt.Params(fill_mode=mode)
    trades, eq, skipped = bt.run(LocalChainSource(synth_dir), "2019-01-01", "2020-12-31", p)
    closed = trades[trades.exit_reason != "open"]
    assert len(skipped) == 0
    assert len(closed) > 80
    # selection matches the rules
    assert closed.entry_delta.between(-0.12, -0.08).all()
    assert closed.entry_dte.between(85, 95).all()
    assert set(closed.exit_reason) <= {"profit_target", "time_21dte"}
    pt = closed[closed.exit_reason == "profit_target"]
    assert (pt.exit_price <= 0.5 * pt.credit + 1e-9).all()
    tm = closed[closed.exit_reason == "time_21dte"]
    assert ((pd.to_datetime(tm.expiry) - pd.to_datetime(tm.exit_date)).dt.days <= 21).all()
    # P&L arithmetic incl. $1/side commissions
    assert np.allclose(closed.pnl, (closed.credit - closed.exit_price) * 100 - 2.0, atol=0.02)
    # final equity = closed P&L - entry commissions of still-open trades + their MTM
    open_t = trades[trades.exit_reason == "open"]
    last = eq.iloc[-1]
    assert last.realized == pytest.approx(closed.pnl.sum() - len(open_t) * 1.0, abs=0.05)
    assert eq.open_positions.max() >= 8
    assert eq.buying_power.max() > 0
    s = bt.report(trades, eq, skipped, tmp_path / mode, mode)
    assert s["full"]["trades"] == len(closed)
    for f in ["trades.csv", "equity_daily.csv", "by_year.csv", "equity_curve.png", "drawdown.png", "pnl_histogram.png"]:
        assert (tmp_path / mode / f).exists()


def test_conservative_is_worse(synth_dir):
    a, _, _ = bt.run(LocalChainSource(synth_dir), "2019-01-01", "2020-12-31", bt.Params(fill_mode="mid"))
    b, _, _ = bt.run(LocalChainSource(synth_dir), "2019-01-01", "2020-12-31", bt.Params(fill_mode="conservative"))
    assert b[b.exit_reason != "open"].pnl.sum() < a[a.exit_reason != "open"].pnl.sum()


class _FakeStore:
    def __init__(self, df):
        self.df = df

    def to_df(self):
        return self.df


class _FakeDatabento:
    """Answers get_range from the synthetic chains, shaped like cbbo-1m to_df()."""

    def __init__(self, chain_dir):
        self.dir = chain_dir
        self.calls = []
        outer = self

        class TS:
            def get_range(self, dataset, schema, symbols, stype_in, start, end):
                outer.calls.append((stype_in, len(symbols), start))
                d = start.tz_convert("America/New_York").date()
                ch = pd.read_parquet(outer.dir / f"{d}.parquet")
                ch["expiration"] = pd.to_datetime(ch["expiration"]).dt.date
                ch["symbol"] = [osi(e, r, k) for e, r, k in zip(ch.expiration, ch.right, ch.strike)]
                if stype_in == "raw_symbol":
                    ch = ch[ch.symbol.isin(set(symbols))]
                # two records per contract; the later one is the close quote
                early = ch.assign(bid=ch.bid * 0.5, ask=ch.ask * 2)
                idx = [end - pd.Timedelta(minutes=2)] * len(early) + [end - pd.Timedelta(minutes=1)] * len(ch)
                both = pd.concat([early, ch])
                df = pd.DataFrame({"symbol": both.symbol.values, "bid_px_00": both.bid.values,
                                   "ask_px_00": both.ask.values}, index=pd.DatetimeIndex(idx, name="ts_recv"))
                return _FakeStore(df)

        class MD:
            def get_cost(self, **kw):
                return 0.01

        self.timeseries, self.metadata = TS(), MD()


def test_databento_source_matches_full_chain(synth_dir, tmp_path):
    """Same trades whether quotes come from full chains or from the targeted Databento
    requests (trimmed entry chains + per-contract quotes + parity spot), and a second
    run is served entirely from the cache."""
    start, end = "2019-01-01", "2019-12-31"
    ref, ref_eq, _ = bt.run(LocalChainSource(synth_dir), start, end, bt.Params())

    src = DatabentoSource(cache_dir=tmp_path / "cache")
    fake = _FakeDatabento(synth_dir)
    src._client = fake
    sessions, closes = bt.sessions_between(start, end)
    src.set_sessions(closes)
    total, _ = src.estimate_cost(sorted(bt.entry_days(sessions)), sessions)
    assert total > 0

    # a gateway timeout on the estimate is retried, then reported instead of crashing
    calls = []

    def flaky(**kw):
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("504 The remote gateway timed out.")
        return 0.01
    fake.metadata.get_cost = flaky
    import data_sources
    data_sources._time.sleep, real_sleep = (lambda s: None), data_sources._time.sleep
    try:
        total2, _ = src.estimate_cost(sorted(bt.entry_days(sessions)), sessions)
        assert total2 == pytest.approx(total)

        def down(**kw):
            raise RuntimeError("504 The remote gateway timed out.")
        fake.metadata.get_cost = down
        total3, why = src.estimate_cost(sorted(bt.entry_days(sessions)), sessions)
        assert total3 is None and "504" in why
    finally:
        data_sources._time.sleep = real_sleep
    got, got_eq, skipped = bt.run(src, start, end, bt.Params())
    assert len(skipped) == 0
    cols = ["entry_date", "strike", "expiry", "credit", "exit_date", "exit_reason", "exit_price", "pnl"]
    pd.testing.assert_frame_equal(ref[cols], got[cols])
    # parity spot from 5 near-ATM pairs (expiry >= 3 days out) is close to the full-chain fit
    m = ref_eq.merge(got_eq, on="date")
    assert (abs(m.spot_x - m.spot_y) / m.spot_x).max() < 0.002
    # trimmed cache stays small
    size = sum(f.stat().st_size for f in (tmp_path / "cache").rglob("*.parquet"))
    assert size / len(sessions) < 60_000

    n = len(fake.calls)
    off = DatabentoSource(cache_dir=tmp_path / "cache", offline=True)
    off.set_sessions(closes)
    again, _, _ = bt.run(off, start, end, bt.Params())
    assert len(fake.calls) == n
    pd.testing.assert_frame_equal(got[cols], again[cols])


def test_stop_loss(synth_dir):
    base, base_eq, _ = bt.run(LocalChainSource(synth_dir), "2019-01-01", "2020-12-31", bt.Params())
    t, eq, _ = bt.run(LocalChainSource(synth_dir), "2019-01-01", "2020-12-31", bt.Params(stop_loss=2.0))
    closed = t[t.exit_reason != "open"]
    stops = closed[closed.exit_reason == "stop_loss"]
    assert len(stops) > 0
    # stopped at the first close at/above 2x credit (fill can be worse after a gap, never better)
    assert (stops.exit_price >= 2.0 * stops.credit - 1e-9).all()
    # same entries as without the stop; only exits differ
    assert list(t.entry_date) == list(base.entry_date) and list(t.strike) == list(base.strike)
    assert closed.pnl.min() >= base[base.exit_reason != "open"].pnl.min()
    s = bt.summarize(t)
    assert s["stop_exits"] == len(stops)


def test_cli_compares_stop_levels(synth_dir, tmp_path, monkeypatch, capsys):
    out = tmp_path / "res"
    monkeypatch.setattr(sys, "argv", ["backtest.py", "--data-dir", str(synth_dir), "--out", str(out),
                                      "--modes", "mid", "--stop-loss", "none,3", "--end", "2019-12-31"])
    bt.main()
    text = (out / "report.txt").read_text()
    assert "mid all" in text and "mid stop3x all" in text and "stops" in text
    assert (out / "mid" / "trades.csv").exists() and (out / "mid_stop3x" / "trades.csv").exists()
