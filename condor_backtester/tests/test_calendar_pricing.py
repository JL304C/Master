from datetime import date, timedelta

import pytest

from condor_bt.calendar import (TradingCalendar, listed_expirations, next_quarterly,
                                third_friday)
from condor_bt.pricing import (SurfaceParams, VolSurface, implied_vol, put_delta,
                               put_price)
from condor_bt.products import get_product


def weekdays(start, end, skip=()):
    d, out = start, []
    while d <= end:
        if d.weekday() < 5 and d not in skip:
            out.append(d)
        d += timedelta(days=1)
    return out


def test_third_friday_and_quarterly():
    assert third_friday(2025, 9) == date(2025, 9, 19)
    assert third_friday(2026, 10) == date(2026, 10, 16)
    assert next_quarterly(date(2025, 9, 20)) == date(2025, 12, 19)
    assert next_quarterly(date(2025, 9, 19)) == date(2025, 9, 19)


def test_holiday_moves_expiration_to_prior_day():
    good_friday = date(2025, 4, 18)
    cal = TradingCalendar(weekdays(date(2025, 1, 2), date(2025, 12, 31), skip={good_friday}))
    exps = [e.date for e in listed_expirations(get_product("XSP"), date(2025, 4, 1), cal)]
    assert date(2025, 4, 17) in exps and good_friday not in exps


def test_mes_lists_only_short_dates():
    cal = TradingCalendar(weekdays(date(2025, 1, 2), date(2025, 12, 31)))
    exps = listed_expirations(get_product("MES"), date(2025, 3, 7), cal)
    assert max((e.date - date(2025, 3, 7)).days for e in exps) <= 63
    xsp = listed_expirations(get_product("XSP"), date(2025, 3, 7), cal)
    assert any(83 <= (e.date - date(2025, 3, 7)).days <= 97 for e in xsp)


def test_quarterly_flag():
    cal = TradingCalendar(weekdays(date(2025, 1, 2), date(2025, 12, 31)))
    exps = {e.date: e for e in listed_expirations(get_product("ES"), date(2025, 7, 1), cal)}
    assert exps[date(2025, 9, 19)].quarterly
    assert exps[date(2025, 8, 15)].third_friday and not exps[date(2025, 8, 15)].quarterly


def test_implied_vol_round_trip():
    px = put_price(5000, 4600, 0.25, 0.04, 0.22)
    assert implied_vol(px, 5000, 4600, 0.25, 0.04) == pytest.approx(0.22, abs=1e-5)


def test_delta_sane():
    d_atm = put_delta(5000, 5000, 0.25, 0.04, 0.015, 0.18, "index")
    assert -0.5 < d_atm < -0.4
    assert put_delta(5000, 4000, 0.25, 0.04, 0.015, 0.18, "index") > -0.05


def test_skew_makes_low_strikes_richer():
    s = VolSurface(SurfaceParams())
    atm = s.atm_vol(0.25, 16, 18)
    assert s.vol(4500, 5000, 0.25, atm) > s.vol(4800, 5000, 0.25, atm) > atm * 0.99
    # term structure interpolates between VIX and VIX3M
    assert s.atm_vol(30 / 365, 16, 20) < s.atm_vol(60 / 365, 16, 20) < s.atm_vol(93 / 365, 16, 20)
