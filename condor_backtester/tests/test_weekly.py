import math
import sys
import types
from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from condor_bt import weekly as wk
from condor_bt.pricing import SurfaceParams, VolSurface, put_price

TODAY = date(2026, 10, 9)
EXP = date(2026, 12, 31)


def model_rows(spot=777.0, exp=EXP, now=datetime(2026, 10, 9, 12, 0), half_spread=0.03):
    """XSP-like put chain from the model: $1 strikes, bid/ask around the model price."""
    t = wk._time_to_expiry(exp, now)
    f = spot * math.exp((0.04 - 0.013) * t)
    surf = VolSurface(SurfaceParams())
    atm = surf.atm_vol(t, 16.0, 18.0)
    rows = []
    for k in range(500, 800):
        px = put_price(f, k, t, 0.04, surf.vol(k, f, t, atm))
        rows.append((float(k), max(px - half_spread, 0.0), px + half_spread))
    return rows, now


@pytest.fixture
def settings(tmp_path):
    return dict(wk.DEFAULTS, journal=str(tmp_path / "journal.csv"))


def test_plan_structure_matches_strategy(settings):
    rows, now = model_rows()
    quotes = wk.quotes_from_prices(rows, 777.0, EXP, now, 0.04, 0.013)
    plan, why = wk.build_plan(quotes, EXP, TODAY, settings)
    assert plan is not None, why
    l1, l2, l3, l4 = plan.strikes
    assert l1 - l2 == 3 and l3 - l4 == 10 and l2 > l3
    assert abs(plan.quotes[0].delta + 0.20) < 0.02
    assert abs(plan.quotes[2].delta + 0.10) < 0.02
    assert plan.natural_credit <= plan.mid_credit <= plan.best_credit
    assert plan.natural_credit == pytest.approx(plan.mid_credit - 4 * 0.03)


def test_money_matches_thinkorswim_screen(settings):
    # The real order: 732/729/690/680 at 0.15 credit, thinkorswim showed $2.64 fees
    m = wk.money(0.15, dict(settings, fee_per_contract=0.66))
    assert m["fees"] == pytest.approx(2.64)
    assert m["net_credit"] == pytest.approx(12.36)
    assert m["max_loss"] == pytest.approx(687.64)
    assert m["max_profit"] == pytest.approx(312.36)


def test_ticket_and_ladder(settings):
    rows, now = model_rows()
    plan, _ = wk.build_plan(wk.quotes_from_prices(rows, 777.0, EXP, now, 0.04, 0.013), EXP, TODAY, settings)
    t = wk.tos_ticket(plan, settings, 0.14)
    assert t.startswith("BUY +1 CONDOR XSP 100 31 DEC 26 ") and t.endswith("PUT @-.14 LMT")
    assert wk.limit_ladder(0.144, 0.10, 3) == [0.14, 0.13, 0.12, 0.11]
    assert wk.limit_ladder(0.12, 0.10, 5) == [0.12, 0.11, 0.10]


def test_skips(settings):
    rows, now = model_rows()
    quotes = wk.quotes_from_prices(rows, 777.0, EXP, now, 0.04, 0.013)
    plan, why = wk.build_plan(quotes, EXP, TODAY, dict(settings, credit_delta=0.19))
    assert plan is None and "overlap" in why
    assert wk.build_plan([], EXP, TODAY, settings)[0] is None
    assert wk.pick_expiration([date(2026, 11, 20), date(2027, 3, 19)], TODAY, 90, 7) is None
    assert wk.pick_expiration([date(2026, 12, 31), date(2027, 1, 8)], TODAY, 90, 7) == date(2027, 1, 8)


def test_record_and_status(settings, capsys):
    a = types.SimpleNamespace(expiration="2026-12-31", strikes="732/729/690/680", fill=0.15, mid=0.14,
                              contracts=1, date="2026-10-08", note="")
    wk.cmd_record(a, settings)
    rows = wk.read_journal(settings["journal"])
    assert rows[0]["max_loss_usd"] == "687.64"
    assert len(wk.open_positions(rows, TODAY)) == 1
    wk.cmd_status(None, settings)
    out = capsys.readouterr().out
    assert "open: 1 of 13" in out and "$688" in out
    assert "-0.010 below mid" in out   # filled 0.01 better than mid


def test_plan_command_end_to_end(settings, monkeypatch, capsys):
    rows, now = model_rows()
    quotes = wk.quotes_from_prices(rows, 777.0, EXP, now, 0.04, 0.013)
    monkeypatch.setattr(wk, "yahoo_chain", lambda s, today: (777.0, [EXP], EXP, quotes))
    a = types.SimpleNamespace(source="yahoo")
    rc = wk.cmd_plan(a, settings)
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "BUY +1 CONDOR XSP 100" in out and "python -m condor_bt.weekly record" in out
    # guard: total risk limit
    rc = wk.cmd_plan(a, dict(settings, max_total_risk=100))
    assert rc == 1 and "max_total_risk" in capsys.readouterr().out


def test_yahoo_falls_back_to_spx_scaled(settings, monkeypatch):
    rows, now = model_rows(spot=7770.0 / 10)
    spx_rows = [(k * 10, b * 10, a * 10) for k, b, a in rows]

    class FakeTicker:
        def __init__(self, sym):
            self.sym = sym
            self.options = () if sym == "^XSP" else (EXP.isoformat(),)

        def history(self, period):
            return pd.DataFrame({"Close": [7770.0]})

        def option_chain(self, e):
            df = pd.DataFrame(spx_rows + [(7775.0, 50.0, 51.0)], columns=["strike", "bid", "ask"])
            return types.SimpleNamespace(puts=df)

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(Ticker=FakeTicker))
    spot, exps, exp, quotes = wk.yahoo_chain(settings, EXP - timedelta(days=88))
    assert spot == pytest.approx(777.0) and exp == EXP
    assert all(q.strike == int(q.strike) for q in quotes)   # 7775 (-> 777.5) dropped
    assert max(q.strike for q in quotes) < 800
