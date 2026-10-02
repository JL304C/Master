"""
Rules for the SPY weekly double calendar, kept free of any data source so the backtest
and a future bot share exactly the same logic.

A double calendar = a put calendar below the price + a call calendar above it:
  sell the near-expiry put / call, buy the same strike at a later expiry, for a net debit.

Structures (entered Tuesday morning, or Wednesday when Tuesday is a market holiday):
  FF  short Friday ~10 DTE, long the next Friday        (the base trade, VIX < 20)
  FM  short Friday ~10 DTE, long the Monday after it    (his alternative long leg)
  WF  short Wednesday ~8 DTE, long the Friday after it  (his high-VIX "Wednesday trick")

Exits (spec): scale out half at +20% of the debit and the rest at +30%; exit everything
when SPY touches either strike; otherwise be out 3 calendar days before a Friday short
expiry (the following Tuesday) or 2 days before a Wednesday one (the Monday).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

MONDAY, TUESDAY, WEDNESDAY, FRIDAY = 0, 1, 2, 4


@dataclass(frozen=True)
class Structure:
    name: str
    short_weekday: int
    short_dte: tuple[int, int]     # DTE range the short expiry must fall in (from the entry day)
    long_gap_days: int             # long expiry = short expiry + this many calendar days
    time_stop_days: int            # be out this many calendar days before the short expiry
    label: str


STRUCTURES = {
    "FF": Structure("FF", FRIDAY, (9, 15), 7, 3, "short Fri ~10 DTE / long next Fri"),
    "FM": Structure("FM", FRIDAY, (9, 15), 3, 3, "short Fri ~10 DTE / long the Monday after"),
    "WF": Structure("WF", WEDNESDAY, (6, 14), 2, 2, "short Wed ~8 DTE / long the Friday after"),
}

VIX_MAX = 20.0                     # "enter only when VIX is low ... ideally under 20"


@dataclass(frozen=True)
class ExitRules:
    name: str
    targets: tuple = ((0.5, 0.20), (0.5, 0.30))   # (fraction of the position, profit as a fraction of the debit)
    touch_stop: bool = True                        # SPY trades at/through either strike -> exit everything


SPEC_EXITS = ExitRules("spec: half +20%, half +30%, touch, time")
EXIT_GRID = [
    SPEC_EXITS,
    ExitRules("all at +30%", targets=((1.0, 0.30),)),
    ExitRules("all at +20%", targets=((1.0, 0.20),)),
    ExitRules("all at +50%", targets=((1.0, 0.50),)),
    ExitRules("no profit target (touch + time stop)", targets=()),
    ExitRules("no touch stop (targets + time stop)", touch_stop=False),
    ExitRules("time stop only", targets=(), touch_stop=False),
]


# --------------------------------------------------------------------------- #
# calendar
# --------------------------------------------------------------------------- #
def entry_days(trading_days) -> list[date]:
    """One entry per week: the Tuesday, or the Wednesday when Tuesday is a holiday."""
    td = set(trading_days)
    out, seen = [], set()
    for d in sorted(td):
        wk = d.isocalendar()[:2]
        if wk in seen:
            continue
        if d.weekday() == TUESDAY or (d.weekday() == WEDNESDAY and d - timedelta(days=1) not in td):
            out.append(d)
            seen.add(wk)
    return out


def listed_expiry(d: date, is_trading_day) -> date:
    """A weekly whose day is a market holiday expires the trading day before (Good Friday -> Thursday)."""
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def expiries(entry: date, s: Structure, is_trading_day):
    """(short, long) listed expiry dates for an entry, or None."""
    lo, hi = s.short_dte
    short = next((entry + timedelta(days=n) for n in range(lo, hi + 1)
                  if (entry + timedelta(days=n)).weekday() == s.short_weekday), None)
    if short is None:
        return None
    long = listed_expiry(short + timedelta(days=s.long_gap_days), is_trading_day)
    short = listed_expiry(short, is_trading_day)
    if long <= short:
        return None                     # e.g. the Monday long leg falls on a holiday
    return short, long


def time_stop_day(entry: date, short_exp: date, s: Structure, trading_days) -> date | None:
    """Last trading day at least `time_stop_days` calendar days before the short expiry."""
    limit = short_exp - timedelta(days=s.time_stop_days)
    cands = [d for d in trading_days if entry < d <= limit]
    return max(cands) if cands else None


def fomc_in_window(entry: date, exit_day: date, fomc_days) -> bool:
    """An FOMC statement day between entry and the planned exit: skip the week
    (the trade would have to close before it anyway)."""
    return any(entry <= f <= exit_day for f in fomc_days)


# --------------------------------------------------------------------------- #
# strikes
# --------------------------------------------------------------------------- #
def strike_targets(spot: float, expected_move: float, mult: float = 1.0) -> tuple[float, float]:
    """Put strike target below the price, call strike target above, at the expected move."""
    return spot - expected_move * mult, spot + expected_move * mult


def pick_strike(target: float, available, below: float | None = None, above: float | None = None):
    """Listed strike nearest the target, strictly below `below` / above `above` when given."""
    ok = [k for k in available if (below is None or k < below) and (above is None or k > above)]
    return min(ok, key=lambda k: (abs(k - target), k)) if ok else None


# --------------------------------------------------------------------------- #
# exits
# --------------------------------------------------------------------------- #
def simulate(debit: float, path, put_k: float, call_k: float, rules: ExitRules):
    """
    Walk a position minute by minute.

    path: list of dicts {ts, hi, lo, val}: SPY high/low in that minute (None if no bar) and the
          value the position could be closed for at the end of it (None if a leg had no quote yet).
          The last element is the time-stop minute.
    Profit targets are resting limit orders: they fill at their limit price once `val` reaches it.
    A strike touch closes the rest at the NEXT minute's value (time to see it and send the order).
    Returns [(fraction, price, reason, ts), ...] or None when the position never had a value.
    """
    remaining, hit, fills = 1.0, set(), []
    last_val, pending_touch = None, False
    for m in path:
        v = m["val"]
        if v is not None:
            last_val = v
        if pending_touch and last_val is not None:
            fills.append((remaining, last_val, "touch", m["ts"]))
            return fills
        if v is not None:
            for k, (frac, tp) in enumerate(rules.targets):
                limit = debit * (1 + tp)
                if k not in hit and v >= limit - 1e-9:
                    hit.add(k)
                    f = min(frac, remaining)
                    fills.append((f, limit, f"target {tp:.0%}", m["ts"]))
                    remaining -= f
            if remaining <= 1e-9:
                return fills
        if rules.touch_stop and m["hi"] is not None and (m["hi"] >= call_k or m["lo"] <= put_k):
            pending_touch = True
    if last_val is None:
        return None
    fills.append((remaining, last_val, "touch" if pending_touch else "time stop", path[-1]["ts"]))
    return fills


def pnl(fills, debit: float, fees: float) -> float:
    """Dollar P&L for one double calendar (100 multiplier)."""
    proceeds = sum(f * p for f, p, _, _ in fills)
    return round((proceeds - debit) * 100 - fees, 2)


def final_reason(fills) -> str:
    return fills[-1][2] if len(fills) == 1 else " + ".join(dict.fromkeys(r for _, _, r, _ in fills))


def exit_ts(fills) -> datetime:
    return fills[-1][3]
