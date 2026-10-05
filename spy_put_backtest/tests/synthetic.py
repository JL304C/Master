"""Synthetic SPY-like option chains for testing the engine only (NOT results).

GBM with a crash, a skewed Black-Scholes vol surface, known r and q, and a
bid/ask spread. Written in the same parquet layout databento_loader.py produces.
"""
import math
from pathlib import Path

import exchange_calendars as xcals
import numpy as np
import pandas as pd
from scipy.special import ndtr

R, Q = 0.03, 0.015


def bs(S, K, T, sigma, right):
    d1 = (np.log(S / K) + (R - Q + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if right == "C":
        return S * math.exp(-Q * T) * ndtr(d1) - K * math.exp(-R * T) * ndtr(d2)
    return K * math.exp(-R * T) * ndtr(-d2) - S * math.exp(-Q * T) * ndtr(-d1)


def expirations(start, end):
    fridays = pd.date_range(start, end + pd.Timedelta(days=200), freq="W-FRI")
    return sorted({d.date() for d in fridays})


def generate(out_dir, start="2019-01-01", end="2020-12-31", seed=7, crash_day="2020-02-24"):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    cal = xcals.get_calendar("XNYS")
    sessions = [s.date() for s in cal.sessions_in_range(start, end)]
    rng = np.random.default_rng(seed)
    S, base_vol = 270.0, 0.14
    exps = expirations(pd.Timestamp(start), pd.Timestamp(end))
    crash = pd.Timestamp(crash_day).date()
    for i, d in enumerate(sessions):
        if i:
            shock = -0.07 if crash <= d < crash + pd.Timedelta(days=21) and rng.random() < 0.5 else 0.0
            S *= math.exp(0.0003 + 0.009 * rng.standard_normal() + shock)
            base_vol = 0.55 if shock else max(0.12, base_vol * 0.97 + 0.12 * 0.03)
        rows = []
        for e in exps:
            dte = (e - d).days
            if dte < 1 or dte > 200:
                continue
            T = dte / 365.0
            for K in np.arange(round(S * 0.5), round(S * 1.3), 1.0):
                m = math.log(K / S) / math.sqrt(T)
                sigma = base_vol * (1 - 1.2 * m + 0.8 * m * m)
                sigma = min(max(sigma, 0.05), 2.0)
                for right in "CP":
                    v = float(bs(S, K, T, sigma, right))
                    if v < 0.01:
                        continue
                    half = max(0.01, 0.02 * v)
                    rows.append((e, right, float(K), round(max(v - half, 0.0), 2), round(v + half, 2)))
        pd.DataFrame(rows, columns=["expiration", "right", "strike", "bid", "ask"]).to_parquet(
            out / f"{d}.parquet", index=False)
    return sessions
