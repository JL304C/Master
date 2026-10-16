"""Offline checks of the strategy math. No network, no Alpaca keys.
Run: python test_offline.py
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import soxl_roll_bot as b


def test_parse_occ():
    p = b.parse_occ_symbol("SOXL261023P00120500")
    assert p == {"underlying": "SOXL", "expiration": date(2026, 10, 23), "right": "P", "strike": 120.5}
    assert b.parse_occ_symbol("SOXL1261023P00012000")["underlying"] == "SOXL1"  # adjusted contract


def test_pick_expirations():
    fri = date(2026, 10, 16)
    exps = [date(2026, 10, 23), date(2026, 10, 30), date(2026, 11, 6), date(2026, 11, 20)]
    assert b.pick_expirations(exps, fri) == (date(2026, 10, 23), date(2026, 10, 30))
    # Good Friday week: trade expires Thursday
    assert b.pick_expirations([date(2026, 4, 2), date(2026, 4, 10)], date(2026, 3, 27)) == (date(2026, 4, 2), date(2026, 4, 10))
    assert b.pick_expirations([date(2026, 11, 20)], fri) == (None, None)


def test_expected_move_matches_spec_example():
    # Strategy doc: AMZN 253.62, 7 DTE, IV 31.19% -> EM ~= 8.74 by formula (ToS showed 8.66)
    S, today, exp = 253.62, date(2026, 9, 14), date(2026, 9, 21)
    T = 7 / 365
    k = 252.5
    c, p = b.bs_price(S, k, T, 0.3119, "C"), b.bs_price(S, k, T, 0.3119, "P")
    quotes = {("C", k): (c - 0.01, c + 0.01), ("P", k): (p - 0.01, p + 0.01)}
    em, iv = b.expected_move(S, quotes, exp, today)
    assert abs(iv - 0.3119) < 0.002, iv
    assert abs(em - 8.74) < 0.08, em


def test_implied_vol_roundtrip():
    for r in "CP":
        px = b.bs_price(150, 140, 14 / 365, 0.85, r)
        assert abs(b.implied_vol(px, 150, 140, 14 / 365, r) - 0.85) < 1e-4


def test_rule_strike_rounds_down():
    strikes = [120, 125, 127, 130, 135]
    assert b.rule_put_strike(150, 21.5, strikes) == 127     # 150 - 21.5 = 128.5
    assert b.rule_put_strike(150, 21.5, [120, 125, 130]) == 125
    assert b.rule_put_strike(150, 100, strikes) is None


def test_roll_strike():
    assert b.roll_strike(139.0, [135, 139, 140]) == 139
    assert b.roll_strike(139.0, [135, 140]) == 140
    assert b.roll_strike(37.21, [36, 37, 38]) == 37


def test_contracts_for():
    assert b.contracts_for(120, 26_000, 100_000) == 2
    assert b.contracts_for(120, 26_000, 13_000) == 1
    assert b.contracts_for(300, 26_000, 100_000) == 0
    assert b.contracts_for(1, 1e9, 1e9) == b.MAX_CONTRACTS


def test_limit_price_ticks():
    assert b.limit_price(0.80, 0.90, "sell", 0.0) == 0.85
    assert b.limit_price(0.80, 0.90, "sell", 1.0) == 0.80
    assert b.limit_price(0.80, 0.91, "buy", 0.0) == 0.86     # 0.855 rounds up
    assert abs(b.limit_price(3.10, 3.40, "sell", 0.0) - 3.25) < 1e-9
    assert abs(b.limit_price(3.10, 3.40, "buy", 1.0) - 3.40) < 1e-9


def test_last_trading_day_of_week():
    week = [date(2026, 10, d) for d in (12, 13, 14, 15, 16, 19)]
    assert b.is_last_trading_day_of_week(date(2026, 10, 16), week)
    assert not b.is_last_trading_day_of_week(date(2026, 10, 15), week)
    holiday = [date(2026, 3, 30), date(2026, 3, 31), date(2026, 4, 1), date(2026, 4, 2), date(2026, 4, 6)]
    assert b.is_last_trading_day_of_week(date(2026, 4, 2), holiday)          # Good Friday closed
    assert not b.is_last_trading_day_of_week(date(2026, 4, 3), holiday)      # market closed


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            try:
                fn()
                print("ok  ", name)
            except AssertionError as e:
                fails += 1
                print("FAIL", name, e)
    sys.exit(1 if fails else 0)
