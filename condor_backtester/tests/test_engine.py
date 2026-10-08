from datetime import date

import pandas as pd
import pytest

from condor_bt.calendar import TradingCalendar, listed_expirations
from condor_bt.chains import SyntheticChain
from condor_bt.config import Config
from condor_bt.engine import Backtester
from condor_bt.metrics import summarize
from condor_bt.payoff import Strikes, condor_pnl_at_expiry
from condor_bt.pricing import VolSurface
from condor_bt.products import get_product

BASE = Config(start_date=date(2020, 10, 1), end_date=date(2025, 10, 1))


def run(market, **kw):
    return Backtester(BASE.with_(**kw), market).run()


@pytest.fixture(scope="module")
def spx(market):
    return run(market)


def test_strikes_ordered_and_deltas_on_target(spx):
    t = spx.trades
    assert len(t) > 200
    assert (t.l1 > t.l2).all() and (t.l2 > t.l3).all() and (t.l3 > t.l4).all()
    assert ((t.l1 - t.l2) == 25).all() and ((t.l3 - t.l4) == 100).all()
    assert (t.d1 + 0.20).abs().max() < 0.02
    assert (t.d3 + 0.10).abs().max() < 0.02
    assert (t.credit_pts > 0).all()


def test_settled_pnl_matches_payoff(spx):
    t = spx.trades[spx.trades.exit_reason == "expiration"]
    for r in t.itertuples():
        pts = condor_pnl_at_expiry(r.settle_price, Strikes(r.l1, r.l2, r.l3, r.l4), r.credit_pts)
        assert r.pnl_usd == pytest.approx(pts * 100 - r.fees_usd, abs=0.06)  # credit_pts is rounded


def test_concurrency_about_13(spx):
    s = summarize(spx)
    assert 12 <= s["max_concurrent_positions"] <= 14
    assert s["worst_case_aggregate_loss_usd"] >= 12 * spx.trades.max_loss_usd.min()


def test_min_credit_filter_skips(market):
    res = run(market, min_credit=50.0)
    assert len(res.trades) == 0
    assert res.skips.skip_reason.str.startswith("credit below").all()


def test_slippage_reduces_credit_by_four_legs(market):
    mid = run(market, end_date=date(2021, 1, 1)).trades
    slip = run(market, end_date=date(2021, 1, 1), pricing="slippage", slippage_per_leg=0.05).trades
    assert (mid.credit_pts - slip.credit_pts).round(6).eq(0.20).all()


def test_mes_cannot_reach_90_dte(market):
    res = run(market, product="MES", end_date=date(2021, 3, 1))
    assert len(res.trades) == 0
    assert res.skips.skip_reason.str.contains("no expiration").all()
    ok = run(market, product="MES", target_dte=49, end_date=date(2021, 3, 1))
    assert len(ok.trades) > 0 and ok.product.usd_per_point == 5


def test_xsp_width_must_fit_strike_grid(market):
    with pytest.raises(ValueError, match="strike step"):
        Backtester(BASE.with_(product="XSP"), market)
    res = run(market, product="XSP", debit_width=30, end_date=date(2021, 6, 1))
    assert (res.trades.l1_native * 10 == res.trades.l1).all()


def test_exits(market):
    res = run(market, take_profit_pct=0.10, end_date=date(2022, 1, 1))
    assert (res.trades.exit_reason == "take_profit").any()
    res = run(market, exit_dte=21, end_date=date(2021, 6, 1))
    closed = res.trades[res.trades.outcome != "open"]
    assert (closed.exit_reason == "exit_dte").all()
    assert ((pd.to_datetime(closed.expiration) - pd.to_datetime(closed.exit_date)).dt.days <= 21).all()


def test_guards(market):
    res = run(market, max_concurrent_positions=5, end_date=date(2022, 1, 1))
    assert res.daily.open_positions.max() <= 5
    assert (res.skips.skip_reason == "max_concurrent_positions").any()


def test_span_margin_rises_in_selloff(market):
    res = run(market, product="ES", dte_tolerance=17)
    t = res.trades
    assert (t.peak_margin_usd <= t.max_loss_usd + 1e-6).all()
    assert t.peak_margin_usd.max() > 3 * t.peak_margin_usd.min()
    assert t.peak_margin_usd.min() == pytest.approx(500)   # the ~$500 floor seen at entry


def test_csv_chain_matches_synthetic(market, tmp_path):
    """Export the model chain to the real-data CSV format, read it back through
    CsvChain, and check the backtest is the same."""
    cfg = BASE.with_(product="XSP", debit_width=30, start_date=date(2021, 1, 4), end_date=date(2021, 3, 1),
                     dte_tolerance=17,
                     product_overrides={"expiration_rules": [{"kind": "third_friday", "max_dte": 120}]})
    prod = get_product("XSP", cfg.product_overrides)
    sub = market.slice(date(2021, 1, 4), date(2021, 7, 1))
    chain = SyntheticChain(prod, sub, VolSurface(cfg.surface), cfg.div_yield)
    cal = TradingCalendar(sub.dates)
    rows = []
    for d in sub.dates:
        for e in listed_expirations(prod, d, cal):
            if e.date > sub.dates[-1]:
                continue
            for q in chain.puts(d, e):
                rows.append({"date": d, "expiration": e.date, "strike": round(q.strike * 0.1, 4), "right": "P",
                             "bid": q.bid * 0.1, "ask": q.ask * 0.1, "delta": q.delta})
    path = tmp_path / "chain.csv"
    pd.DataFrame(rows).to_csv(path, index=False)
    a = Backtester(cfg, sub).run().trades
    b = Backtester(cfg, sub, str(path)).run().trades
    assert len(a) == len(b) > 0
    cols = ["l1", "l2", "l3", "l4", "credit_pts", "pnl_usd"]
    pd.testing.assert_frame_equal(a[cols].reset_index(drop=True), b[cols].reset_index(drop=True),
                                  check_exact=False, atol=1e-6)
