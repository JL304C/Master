"""Generate a SIMULATED market CSV (SPX/VIX/VIX3M/rate) for tests and demos.

This is NOT market history. It exists so the engine can be exercised end to
end without downloading anything. Each path has a few injected sell-offs
(a slow bear market and a fast crash) so the risk reports have something to
show. Use ``scripts/fetch_market_data.py`` for real data.
"""
from __future__ import annotations

import argparse
import math
import os

import numpy as np
import pandas as pd


def simulate(start="2020-10-01", end="2026-03-31", s0=3380.0, seed=5,
             crashes=(("2022-01-03", 190, -0.22), ("2025-04-01", 6, -0.15))) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    days = pd.bdate_range(start, end)
    n = len(days)
    log_vix = np.empty(n)
    log_vix[0] = math.log(20.0)
    mu_lv, kappa, eta, rho = math.log(17.0), 6.0, 0.8, -0.75
    dt = 1 / 252
    # drift schedule: injected sell-offs override the normal drift
    drift = np.full(n, 0.08)
    vix_target = np.full(n, mu_lv)
    for d0, length, total in crashes:
        i0 = days.searchsorted(pd.Timestamp(d0))
        i1 = min(i0 + length, n)
        if i0 < n:
            drift[i0:i1] = total / ((i1 - i0) * dt)
            # fast crashes pull VIX toward ~45, slow bears toward ~28
            vix_target[i0:i1] = math.log(45.0 if length < 20 else 28.0)
    s = np.empty(n)
    s[0] = s0
    z1 = rng.standard_normal(n)
    z2 = rho * z1 + math.sqrt(1 - rho * rho) * rng.standard_normal(n)
    for i in range(1, n):
        lv = log_vix[i - 1]
        k_i = kappa * (12 if vix_target[i] > mu_lv + 0.6 else 1)
        log_vix[i] = lv + k_i * (vix_target[i] - lv) * dt + eta * math.sqrt(dt) * z2[i]
        log_vix[i] = min(max(log_vix[i], math.log(9.0)), math.log(80.0))
        vol = math.exp(log_vix[i - 1]) / 100 * 0.85
        s[i] = s[i - 1] * math.exp((drift[i] - 0.5 * vol * vol) * dt + vol * math.sqrt(dt) * z1[i])
    vix = np.exp(log_vix)
    vix3m = vix + 0.35 * (19.5 - vix) + rng.normal(0, 0.3, n)   # contango in calm, backwardation in stress
    opens = np.r_[s[0], s[:-1]] * np.exp(rng.normal(0, 0.002, n))
    rate = np.interp(np.arange(n), [0, n * 0.3, n * 0.55, n], [0.001, 0.01, 0.05, 0.04])
    return pd.DataFrame({"date": days.date, "open": opens.round(2), "close": s.round(2),
                         "vix": vix.round(2), "vix3m": vix3m.round(2), "rate": rate.round(5)})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join("data", "simulated_market.csv"))
    ap.add_argument("--seed", type=int, default=5)
    a = ap.parse_args()
    df = simulate(seed=a.seed)
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    df.to_csv(a.out, index=False)
    print(f"wrote SIMULATED market data ({len(df)} rows) to {a.out}")


if __name__ == "__main__":
    main()
