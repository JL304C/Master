import pytest

from condor_bt.payoff import (Strikes, condor_pnl_at_expiry, max_loss, max_profit,
                              outcome_bucket)

# Spec section 5 example: 25-wide debit spread, 100-wide credit spread, C = 2.00, ES $50/pt
K = Strikes(l1=5000, l2=4975, l3=4800, l4=4700)
C = 2.0
ES = 50


def test_spec_dollar_numbers():
    assert max_profit(K, C) * ES == pytest.approx(1350)
    assert max_loss(K, C) * ES == pytest.approx(3650)
    assert condor_pnl_at_expiry(5200, K, C) * ES == pytest.approx(100)


@pytest.mark.parametrize("price, expected", [
    (6000, C),                 # far above: keep the credit
    (5000, C),                 # at L1
    (4990, C + 10),            # inside the debit spread
    (4975, C + 25),            # tent starts
    (4900, C + 25),            # tent
    (4800, C + 25),            # tent ends at L3
    (4750, C + 25 - 50),       # inside the credit spread
    (4700, C + 25 - 100),      # max loss from L4 down
    (3000, C + 25 - 100),
])
def test_payoff_zones(price, expected):
    assert condor_pnl_at_expiry(price, K, C) == pytest.approx(expected)


def test_payoff_matches_formula_everywhere():
    for price in range(4500, 5200, 7):
        pnl = condor_pnl_at_expiry(price, K, C)
        assert -max_loss(K, C) - 1e-9 <= pnl <= max_profit(K, C) + 1e-9


def test_equal_inner_strikes_still_valid():
    k = Strikes(5000, 4975, 4975, 4875)
    assert max_profit(k, 1.0) == 26
    assert condor_pnl_at_expiry(4975, k, 1.0) == pytest.approx(26)
    assert condor_pnl_at_expiry(4800, k, 1.0) == pytest.approx(-max_loss(k, 1.0))


def test_buckets():
    assert outcome_bucket(5100, K) == "above"
    assert outcome_bucket(4990, K) == "partial_upper"
    assert outcome_bucket(4900, K) == "tent"
    assert outcome_bucket(4750, K) == "partial_lower"
    assert outcome_bucket(4700, K) == "max_loss"
