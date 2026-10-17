"""Basic indicators. All take a bar DataFrame (open/high/low/close/volume) or a Series."""
from __future__ import annotations

import numpy as np
import pandas as pd


def true_range(df: pd.DataFrame) -> pd.Series:
    prev = df["close"].shift(1)
    return pd.concat([df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1).max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder's ATR."""
    return true_range(df).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def session_vwap(df: pd.DataFrame) -> pd.Series:
    """VWAP that resets each session date, using the bar's typical price."""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    day = df.index.date
    pv = (tp * df["volume"]).groupby(day).cumsum()
    v = df["volume"].groupby(day).cumsum()
    return pv / v.replace(0, np.nan)


def bollinger_upper(s: pd.Series, n: int = 20, k: float = 2.0) -> pd.Series:
    return sma(s, n) + k * s.rolling(n, min_periods=n).std(ddof=0)


def bollinger_lower(s: pd.Series, n: int = 20, k: float = 2.0) -> pd.Series:
    return sma(s, n) - k * s.rolling(n, min_periods=n).std(ddof=0)


def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    """Wilder's RSI."""
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))
