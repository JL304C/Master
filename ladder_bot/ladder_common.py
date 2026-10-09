"""
Shared math for the 1-1-1-2 Put Step-Down Ladder (DT Options), used by both
ladder_backtest.py and ladder_bot.py so the backtest and the paper bot pick
strikes and compute risk exactly the same way.

Structure (all puts, same ~90 DTE monthly expiration):
    K1  BUY  1  ~22.5 delta (top)
    K2  SELL 1  ~17 delta
    K3  BUY  1  ~13 delta
    K4  SELL 2  ~10 delta (bottom)
Strikes equally spaced: K3 = K4 + w, K2 = K4 + 2w, K1 = K4 + 3w.

Payoff at expiration (per share):
    S >= K1        : credit
    K4 <= S <= K1  : >= credit (peaks at S = K4)
    S = K4         : 2w + credit  (= W1 - W2 + W3 + credit, W1=w, W2=2w, W3=3w)
    S <  K4        : 2w + credit - (K4 - S)   -> one net short put
Lower breakeven = K4 - (2w + credit).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from datetime import date, timedelta

MULTIPLIER = 100
TARGET_DELTAS = (0.225, 0.17, 0.13, 0.10)   # K1..K4, absolute put deltas
RATIOS = (1, -1, 1, -2)                      # +buy / -sell, per ladder

# --- Volatility-surface model (used only where real chain data is missing) ---
# VIX is a 30-day number; the ladder is ~90 DTE. 90-day vol mean-reverts toward
# a long-run level, so the 90-day ATM vol is pulled part way from VIX to LT_VOL.
LT_VOL = 0.20
TERM_PULL = 0.35          # 0 = use VIX as-is, 1 = always LT_VOL
ATM_VS_VIX = 0.92         # VIX sits above ATM IV because it includes skew
SKEW_PER_SD = 0.30        # put IV rises 30% of ATM IV per 1 SD out of the money


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_put(S: float, K: float, T: float, r: float, q: float, sigma: float) -> float:
    if T <= 0 or sigma <= 0:
        return max(0.0, K - S)
    st = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / st
    d2 = d1 - st
    return K * math.exp(-r * T) * norm_cdf(-d2) - S * math.exp(-q * T) * norm_cdf(-d1)


def bs_put_delta(S: float, K: float, T: float, r: float, q: float, sigma: float) -> float:
    """Absolute value of put delta."""
    if T <= 0:
        return 1.0 if S < K else 0.0
    st = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / st
    return math.exp(-q * T) * norm_cdf(-d1)


def atm_vol_from_vix(vix: float) -> float:
    v = vix / 100.0
    return ATM_VS_VIX * (v + TERM_PULL * (LT_VOL - v))


def put_iv(S: float, K: float, T: float, atm: float, skew: float = SKEW_PER_SD) -> float:
    """Skewed put IV: rises linearly with how many SDs the strike is below spot."""
    sd_otm = max(0.0, math.log(S / K)) / (atm * math.sqrt(max(T, 1e-6)))
    return atm * (1.0 + skew * sd_otm)


def third_friday(year: int, month: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(4 - d.weekday()) % 7)
    return d + timedelta(days=14)


def monthly_expiry_near(today: date, dte_target: int = 90) -> date:
    """Closest standard monthly (3rd-Friday) expiration to dte_target days out."""
    best = None
    y, m = today.year, today.month
    for _ in range(8):
        e = third_friday(y, m)
        if e > today and (best is None or abs((e - today).days - dte_target) < abs((best - today).days - dte_target)):
            best = e
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return best


@dataclass
class Ladder:
    k1: float
    k2: float
    k3: float
    k4: float
    width: float
    credit: float          # per share, positive = credit received
    raw_strikes: tuple     # strikes chosen purely by delta, before snapping

    @property
    def strikes(self):
        return (self.k1, self.k2, self.k3, self.k4)

    def max_profit(self) -> float:
        """$ per ladder at S = K4: (W1 - W2 + W3) x 100 + credit."""
        w1, w2, w3 = self.k3 - self.k4, self.k2 - self.k4, self.k1 - self.k4
        return (w1 - w2 + w3) * MULTIPLIER + self.credit * MULTIPLIER

    def breakeven(self) -> float:
        return self.k4 - self.max_profit() / MULTIPLIER

    def payoff(self, S: float) -> float:
        """$ per ladder at expiration, including the opening credit."""
        legs = sum(r * max(0.0, k - S) for r, k in zip(RATIOS, self.strikes))
        return (legs + self.credit) * MULTIPLIER

    def drop_losses(self, spot: float, drops=(0.20, 0.30, 0.40)) -> dict:
        return {f"pnl_if_down_{int(d * 100)}pct": round(self.payoff(spot * (1 - d)), 2) for d in drops}

    def cash_secured_requirement(self) -> float:
        """Cash needed if the extra short K4 put must be cash-secured: the most
        the legs can lose at expiration (SPY -> 0), premium left out -- the
        same "universal spread rule" Alpaca applies to multi-leg orders.
        = -(K1 - K2 + K3 - 2*K4) x 100 = (K4 - 2w) x 100."""
        return -sum(r * k for r, k in zip(RATIOS, self.strikes)) * MULTIPLIER

    def max_loss_if_zero(self) -> float:
        return self.payoff(0.0)

    def regt_requirement(self, spot: float, naked_put_price: float) -> float:
        """Reg-T style margin for the one uncovered short put (what a margin
        broker would charge): max(20% of spot - OTM amount, 10% of strike)
        + premium, less the net credit received."""
        otm = max(0.0, spot - self.k4)
        req = max(0.20 * spot - otm, 0.10 * self.k4) + naked_put_price
        return max(0.0, req - self.credit) * MULTIPLIER

    def to_dict(self):
        d = asdict(self)
        d["raw_strikes"] = list(self.raw_strikes)
        return d


def strike_for_delta(S, T, r, q, atm, target, inc=1.0, skew=SKEW_PER_SD):
    """Strike (on the `inc` grid) whose skewed-IV put delta is closest to target."""
    lo, hi = S * 0.40, S
    k = round(S / inc) * inc
    best, best_err = k, 9.9
    while k > lo:
        d = bs_put_delta(S, k, T, r, q, put_iv(S, k, T, atm, skew))
        err = abs(d - target)
        if err < best_err:
            best, best_err = k, err
        elif d < target:
            break
        k -= inc
    return best


def build_ladder(S, T, r, q, atm, inc=1.0, skew=SKEW_PER_SD, price_fn=None):
    """Pick strikes by delta, then snap to equal spacing anchored on the
    10-delta bottom strike. price_fn(K) -> mid price; defaults to the model."""
    raw = tuple(strike_for_delta(S, T, r, q, atm, d, inc, skew) for d in TARGET_DELTAS)
    k4 = raw[3]
    w = max(inc, round((raw[0] - k4) / 3.0 / inc) * inc)
    ks = (k4 + 3 * w, k4 + 2 * w, k4 + w, k4)
    if price_fn is None:
        price_fn = lambda K: bs_put(S, K, T, r, q, put_iv(S, K, T, atm, skew))
    credit = -sum(rr * price_fn(k) for rr, k in zip(RATIOS, ks))
    return Ladder(*ks, width=w, credit=credit, raw_strikes=raw)


def ladder_value(lad: Ladder, S, T, r, q, atm, skew=SKEW_PER_SD) -> float:
    """Mark-to-market $ per ladder of the open legs (positive = asset).
    Closing P&L = value + credit*100."""
    return sum(rr * bs_put(S, k, T, r, q, put_iv(S, k, T, atm, skew)) for rr, k in zip(RATIOS, lad.strikes)) * MULTIPLIER
