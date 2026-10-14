"""Weekly order helper for the XSP broken-wing put condor.

It never sends orders. It reads the live XSP put chain, picks the four
strikes the same way the backtest does, checks the credit, and prints the
order to enter in thinkorswim, plus a short limit-price ladder.

    python -m condor_bt.weekly plan                 # what to place this week
    python -m condor_bt.weekly record 2026-12-31 732/729/690/680 0.15 --mid 0.14
    python -m condor_bt.weekly status               # open positions, total risk, fills vs mid

All prices here are native XSP (what thinkorswim shows): 0.15 = $15 per
contract. Widths are in XSP points (5 and 20 = 50 and 200 SPX points; set in configs/weekly_xsp.yaml).

Chain sources (``--source``):
    yahoo   free, no setup (pip install yfinance). Quotes may be ~15 min delayed.
            Delta is computed from each put's mid price.
    schwab  your Schwab account's live chain with Schwab's own deltas.
            Needs a free developer app; see README "Schwab API (optional)".
    auto    schwab if its environment variables are set, otherwise yahoo.
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime

import yaml

from .pricing import implied_vol, put_delta

DEFAULTS = {
    "symbol": "XSP",
    "target_dte": 90,
    "dte_tolerance": 7,
    "debit_delta": 0.20,
    "debit_width": 3,        # XSP points (= 30 SPX points; 25 isn't on the $1 grid)
    "credit_delta": 0.10,
    "credit_width": 10,      # XSP points (= 100 SPX points)
    "min_credit": 0.10,      # skip the week below this mid credit (fees are ~$2.64)
    "ladder_steps": 3,       # how many 0.01 steps below the starting limit to suggest
    "contracts": 1,
    "fee_per_contract": 0.66,  # Schwab $0.65 + ~$0.01 regulatory, per leg
    "max_open_positions": 13,
    "max_total_risk": 9000,  # dollars of max loss across all open positions
    "journal": os.path.join("journal", "xsp_journal.csv"),
    "risk_free_rate": 0.04,
    "div_yield": 0.013,
    "source": "auto",
}

JOURNAL_FIELDS = ["entry_date", "expiration", "l1", "l2", "l3", "l4", "contracts",
                  "fill_credit", "mid_credit", "fees_usd", "max_loss_usd", "max_profit_usd", "note"]


@dataclass(frozen=True)
class PutQuote:
    strike: float
    bid: float
    ask: float
    delta: float   # negative

    @property
    def mid(self) -> float:
        return 0.5 * (self.bid + self.ask)


@dataclass(frozen=True)
class Plan:
    expiration: date
    dte: int
    strikes: tuple[float, float, float, float]
    quotes: tuple[PutQuote, PutQuote, PutQuote, PutQuote]
    mid_credit: float
    natural_credit: float     # if every leg filled at the worse side
    best_credit: float        # if every leg filled at the better side
    suspects: dict = None     # strike -> bad quote that was replaced by an estimate


def load_settings(path: str | None) -> dict:
    s = dict(DEFAULTS)
    if path and os.path.exists(path):
        with open(path) as fh:
            user = yaml.safe_load(fh) or {}
        unknown = set(user) - set(DEFAULTS)
        if unknown:
            raise ValueError(f"unknown keys in {path}: {sorted(unknown)}")
        s.update(user)
    return s


# --------------------------------------------------------------------- selection
def candidate_expirations(expirations: list[date], today: date, target: int, tol: int) -> list[date]:
    """Expirations within target +/- tol DTE, closest to target first."""
    ok = [e for e in expirations if abs((e - today).days - target) <= tol]
    return sorted(ok, key=lambda e: (abs((e - today).days - target), e))


def pick_expiration(expirations: list[date], today: date, target: int, tol: int) -> date | None:
    c = candidate_expirations(expirations, today, target, tol)
    return c[0] if c else None


def _line(x0, y0, x1, y1, x):
    return y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def find_bad_quotes(quotes: list[PutQuote], abs_tol: float = 0.08, rel_tol: float = 0.15) -> dict:
    """Flag quotes that are out of line with nearby strikes (stale or bad data).

    Put prices rise smoothly with strike, so each mid is compared with the
    median of three estimates: interpolated from the strikes on either side,
    and extrapolated from the two strikes below and the two above. Returns
    {strike: estimated PutQuote} for each bad quote.
    """
    qs = sorted(quotes, key=lambda q: q.strike)
    bad = {}
    for i in range(1, len(qs) - 1):
        q = qs[i]
        preds = [_line(qs[i - 1].strike, qs[i - 1].mid, qs[i + 1].strike, qs[i + 1].mid, q.strike)]
        if i >= 2:
            preds.append(_line(qs[i - 2].strike, qs[i - 2].mid, qs[i - 1].strike, qs[i - 1].mid, q.strike))
        if i + 2 < len(qs):
            preds.append(_line(qs[i + 1].strike, qs[i + 1].mid, qs[i + 2].strike, qs[i + 2].mid, q.strike))
        expect = sorted(preds)[len(preds) // 2]
        if abs(q.mid - expect) > max(abs_tol, rel_tol * expect):
            lo, hi = qs[i - 1], qs[i + 1]
            half = 0.25 * ((lo.ask - lo.bid) + (hi.ask - hi.bid))
            delta = _line(lo.strike, lo.delta, hi.strike, hi.delta, q.strike)
            bad[q.strike] = (q, PutQuote(q.strike, max(expect - half, 0.0), expect + half, delta))
    return bad


DELTA_TOLERANCE = 0.03   # L1/L3 must be within this of the target delta


def chain_summary(quotes: list[PutQuote]) -> str:
    if not quotes:
        return "chain: empty"
    ks = sorted(q.strike for q in quotes)
    priced = sum(1 for q in quotes if q.ask > 0)
    with_delta = [q for q in quotes if math.isfinite(q.delta)]
    dr = (f", deltas {max(q.delta for q in with_delta):.3f} to {min(q.delta for q in with_delta):.3f}"
          if with_delta else "")
    return (f"chain: {len(ks)} strikes {ks[0]:g}-{ks[-1]:g}, {priced} with bid/ask, "
            f"{len(with_delta)} with delta{dr}")


def build_plan(quotes: list[PutQuote], expiration: date, today: date, s: dict) -> tuple[Plan | None, str]:
    """Same rules as the backtest: L1 ~debit_delta, L2 = L1 - debit_width,
    L3 ~credit_delta, L4 = L3 - credit_width, all must be listed, L3 < L2.
    Bad quotes are not used to pick strikes; if a leg's quote is bad, its
    price is estimated from the neighboring strikes and the plan says so."""
    priced = [q for q in quotes if q.ask > 0 and q.bid <= q.ask]
    if not [q for q in priced if math.isfinite(q.delta) and q.delta < 0]:
        return None, "no usable put quotes (market closed or data source empty?)"
    bad = find_bad_quotes(priced)
    by_strike = {q.strike: (bad[q.strike][1] if q.strike in bad else q) for q in priced}
    # strikes are picked by delta, so only quotes with a delta qualify as L1/L3
    good = [q for q in priced if q.strike not in bad and math.isfinite(q.delta) and q.delta < 0]
    q1 = min(good, key=lambda q: abs(q.delta + s["debit_delta"]))
    q3 = min(good, key=lambda q: abs(q.delta + s["credit_delta"]))
    for q, target, name in ((q1, s["debit_delta"], "L1"), (q3, s["credit_delta"], "L3")):
        if abs(q.delta + target) > DELTA_TOLERANCE:
            return None, (f"no strike near {target:.2f} delta for {name}: closest is {q.strike:g} at "
                          f"{q.delta:.3f} (incomplete chain? {chain_summary(quotes)})")
    l1, l3 = q1.strike, q3.strike
    l2, l4 = l1 - s["debit_width"], l3 - s["credit_width"]
    if l3 >= l2:
        return None, f"strike overlap: 10-delta strike {l3:g} is not below {l2:g}; skip this week"
    missing = [k for k in (l2, l4) if k not in by_strike]
    if missing:
        return None, (f"no quote for strike(s) {[f'{k:g}' for k in missing]} "
                      f"(not listed, or no bid/ask yet; run during market hours). {chain_summary(quotes)}")
    legs = (q1, by_strike[l2], q3, by_strike[l4])
    signs = (1, -1, -1, 1)   # buy L1, sell L2, sell L3, buy L4
    mid = -sum(sg * q.mid for sg, q in zip(signs, legs))
    natural = -sum(sg * (q.ask if sg > 0 else q.bid) for sg, q in zip(signs, legs))
    best = -sum(sg * (q.bid if sg > 0 else q.ask) for sg, q in zip(signs, legs))
    suspects = {k: bad[k][0] for k in (l1, l2, l3, l4) if k in bad}
    plan = Plan(expiration, (expiration - today).days, (l1, l2, l3, l4), legs, mid, natural, best, suspects)
    return plan, ""


def money(plan_credit: float, s: dict) -> dict:
    n = s["contracts"]
    fees = 4 * n * s["fee_per_contract"]
    width_d, width_c = s["debit_width"], s["credit_width"]
    return {
        "fees": fees,
        "net_credit": plan_credit * 100 * n - fees,
        "max_profit": (width_d + plan_credit) * 100 * n - fees,
        "max_loss": (width_c - width_d - plan_credit) * 100 * n + fees,
    }


def limit_ladder(mid: float, floor: float, steps: int) -> list[float]:
    start = math.floor(mid * 100 + 1e-6) / 100   # first limit is the mid, cut to a whole cent
    out = []
    for i in range(steps + 1):
        p = round(start - 0.01 * i, 2)
        if p < floor - 1e-9:
            break
        out.append(p)
    return out


def tos_ticket(plan: Plan, s: dict, limit: float) -> str:
    l1, l2, l3, l4 = (f"{k:g}" for k in plan.strikes)
    exp = plan.expiration.strftime("%d %b %y").upper()
    return (f"BUY +{s['contracts']} CONDOR {s['symbol']} 100 {exp} "
            f"{l1}/{l2}/{l3}/{l4} PUT @-{limit:.2f} LMT".replace("@-0.", "@-."))


# --------------------------------------------------------------------- sources
def _time_to_expiry(exp: date, now: datetime) -> float:
    close = datetime(exp.year, exp.month, exp.day, 16, 0)
    return max((close - now).total_seconds(), 3600.0) / (365.0 * 86400.0)


def quotes_from_prices(rows, spot: float, exp: date, now: datetime, r: float, q: float) -> list[PutQuote]:
    """rows: iterable of (strike, bid, ask). Delta from IV implied by the mid."""
    t = _time_to_expiry(exp, now)
    f = spot * math.exp((r - q) * t)
    out = []
    for k, bid, ask in rows:
        if not (ask and ask > 0) or bid is None or bid < 0 or not math.isfinite(k):
            continue
        mid = 0.5 * (bid + ask)
        vol = implied_vol(mid, f, k, t, r)
        if vol is None:
            continue
        out.append(PutQuote(float(k), float(bid), float(ask), put_delta(f, k, t, r, q, vol, "index")))
    return out


def yahoo_chain(s: dict, today: date):
    """Yahoo Finance chain. If Yahoo has no XSP options, fall back to SPX
    scaled by 1/10 (SPX strikes on a 10-point grid map to $1 XSP strikes)."""
    try:
        import yfinance as yf
    except ImportError:
        sys.exit("yfinance is not installed: pip install yfinance")
    tk, scale = yf.Ticker("^" + s["symbol"]), 1.0
    exps = [date.fromisoformat(e) for e in (tk.options or ())]
    if not exps and s["symbol"].upper() == "XSP":
        print("Note: Yahoo has no XSP options; using SPX quotes / 10. "
              "Prices are approximate; check the mid in thinkorswim.\n")
        tk, scale = yf.Ticker("^SPX"), 0.1
        exps = [date.fromisoformat(e) for e in (tk.options or ())]
    if not exps:
        sys.exit(f"Yahoo returned no option expirations for ^{s['symbol']}. "
                 "Try --source schwab, or run during market hours.")
    hist = tk.history(period="5d")
    if hist.empty:
        sys.exit("Yahoo returned no index price")
    spot = float(hist["Close"].iloc[-1]) * scale
    chains = {}
    for exp in candidate_expirations(exps, today, s["target_dte"], s["dte_tolerance"]):
        puts = tk.option_chain(exp.isoformat()).puts
        rows = [(k * scale, b * scale, a_ * scale)
                for k, b, a_ in zip(puts["strike"].astype(float), puts["bid"].astype(float),
                                    puts["ask"].astype(float))
                if scale == 1.0 or abs(k * scale - round(k * scale)) < 1e-9]
        chains[exp] = quotes_from_prices(rows, spot, exp, datetime.now(), s["risk_free_rate"], s["div_yield"])
    return spot, exps, chains


def schwab_configured() -> bool:
    return all(os.environ.get(k) for k in ("SCHWAB_API_KEY", "SCHWAB_APP_SECRET"))


SCHWAB_LOGIN_MAX_AGE_DAYS = 6.5   # Schwab refresh tokens expire after 7 days


def _schwab_token_age_days(path: str) -> float | None:
    import json
    import time
    try:
        with open(path) as fh:
            created = json.load(fh).get("creation_timestamp")
        return None if created is None else (time.time() - float(created)) / 86400.0
    except (OSError, ValueError):
        return None


def _restore_stdlib_ssl() -> str | None:
    """Undo a global ``truststore`` injection (e.g. from the pip-system-certs
    package). It replaces ssl.SSLContext process-wide, which broke both the
    automatic login's local server and the token request on the user's PC.
    Returns the name of the module that was undone, if any."""
    import importlib
    import ssl
    owner = getattr(ssl.SSLContext, "__module__", "")
    if "truststore" not in owner:
        return None
    pkg = owner.rsplit("._api", 1)[0]          # pip._vendor.truststore or truststore
    try:
        importlib.import_module(pkg).extract_from_ssl()
    except Exception:
        return None
    return pkg if "truststore" not in getattr(ssl.SSLContext, "__module__", "") else None


def schwab_client():
    """Saved token if it's fresh; otherwise the copy-paste login, which needs
    no local web server (the automatic browser login fails on some Windows
    Python installs with a certificate error)."""
    _restore_stdlib_ssl()
    try:
        import schwab
    except ImportError:
        sys.exit("schwab-py is not installed: pip install schwab-py")
    key, secret = os.environ["SCHWAB_API_KEY"], os.environ["SCHWAB_APP_SECRET"]
    callback = os.environ.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8182")
    token = os.environ.get("SCHWAB_TOKEN_PATH", os.path.join("journal", "schwab_token.json"))
    os.makedirs(os.path.dirname(token) or ".", exist_ok=True)
    age = _schwab_token_age_days(token)
    if age is not None and age < SCHWAB_LOGIN_MAX_AGE_DAYS:
        return schwab.auth.client_from_token_file(token, key, secret)
    if age is not None:
        print(f"Schwab login is {age:.1f} days old; Schwab requires a new login every 7 days.")
    print("Schwab login: open the link below, sign in with your brokerage login, approve access.\n"
          "Your browser will then show an error page at 127.0.0.1. That's expected:\n"
          "copy the WHOLE address from the address bar and paste it here.")
    return schwab.auth.client_from_manual_flow(key, secret, callback, token)


def cmd_diagnose(a, s):
    import platform
    import ssl
    print("python", platform.python_version(), platform.system())
    print("ssl.SSLContext before:", ssl.SSLContext.__module__)
    undone = _restore_stdlib_ssl()
    print("undid truststore injection from:", undone)
    print("ssl.SSLContext after: ", ssl.SSLContext.__module__)
    for mod in ("schwab", "authlib", "httpx", "truststore", "pip_system_certs", "yfinance"):
        try:
            m = __import__(mod)
            print(f"{mod:<17} {getattr(m, '__version__', 'installed')}")
        except Exception as exc:
            print(f"{mod:<17} not available ({type(exc).__name__})")
    print("SCHWAB_API_KEY set:", bool(os.environ.get("SCHWAB_API_KEY")),
          "  SCHWAB_APP_SECRET set:", bool(os.environ.get("SCHWAB_APP_SECRET")))
    tok = os.environ.get("SCHWAB_TOKEN_PATH", os.path.join("journal", "schwab_token.json"))
    print("token file:", tok, "exists" if os.path.exists(tok) else "missing",
          f"(age {_schwab_token_age_days(tok):.1f} days)" if _schwab_token_age_days(tok) is not None else "")
    return 0


def schwab_login(a, s):
    if not schwab_configured():
        sys.exit("set SCHWAB_API_KEY and SCHWAB_APP_SECRET first (see README)")
    token = os.environ.get("SCHWAB_TOKEN_PATH", os.path.join("journal", "schwab_token.json"))
    if os.path.exists(token):
        os.remove(token)   # force a fresh login
    schwab_client()
    print(f"logged in; token saved to {token}")
    return 0


def schwab_chain(s: dict, today: date):
    from datetime import timedelta
    client = schwab_client()
    lo = today + timedelta(days=s["target_dte"] - s["dte_tolerance"])
    hi = today + timedelta(days=s["target_dte"] + s["dte_tolerance"])
    # ask for every strike: without strike_count/range Schwab may return only a
    # band of strikes near the market, which misses the ~10-delta wing
    resp = client.get_option_chain("$" + s["symbol"], contract_type=client.Options.ContractType.PUT,
                                   strike_count=500, strike_range=client.Options.StrikeRange.ALL,
                                   from_date=lo, to_date=hi, include_underlying_quote=True)
    resp.raise_for_status()
    data = resp.json()
    spot = float(data.get("underlyingPrice") or data["underlying"]["last"])
    by_exp: dict[date, list[PutQuote]] = {}
    for key, strikes in (data.get("putExpDateMap") or {}).items():
        exp = date.fromisoformat(key.split(":")[0])
        for k, contracts in strikes.items():
            c = contracts[0]
            d = c.get("delta")
            # Schwab sends -999 / NaN when it has no greek (e.g. before the open);
            # keep the strike anyway, it can still be a priced L2/L4 leg
            d = float(d) if d is not None and math.isfinite(d) and -1 < d < 0 else math.nan
            by_exp.setdefault(exp, []).append(
                PutQuote(float(k), float(c.get("bid") or 0), float(c.get("ask") or 0), d))
    exps = sorted(by_exp)
    cands = candidate_expirations(exps, today, s["target_dte"], s["dte_tolerance"])
    return spot, exps, {e: by_exp[e] for e in cands}


# --------------------------------------------------------------------- journal
def read_journal(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def append_journal(path: str, row: dict):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    new = not os.path.exists(path)
    with open(path, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=JOURNAL_FIELDS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in JOURNAL_FIELDS})


def open_positions(rows: list[dict], today: date) -> list[dict]:
    return [r for r in rows if date.fromisoformat(r["expiration"]) >= today]


# --------------------------------------------------------------------- commands
def _market_hours_note() -> str | None:
    """Warn outside 9:30-16:00 New York time; quotes are missing or stale then."""
    try:
        from zoneinfo import ZoneInfo
        ny = datetime.now(ZoneInfo("America/New_York"))
    except Exception:          # no tz database (Windows without tzdata): skip the check
        return None
    if ny.weekday() >= 5 or not ((9, 30) <= (ny.hour, ny.minute) < (16, 0)):
        return (f"WARNING: it's {ny:%a %H:%M} in New York and the options market is closed "
                "(9:30-16:00 ET). Quotes may be missing or stale; run it during market hours.")
    return None


def cmd_plan(a, s):
    today = date.today()
    src = s["source"] if a.source is None else a.source
    if src == "auto":
        src = "schwab" if schwab_configured() else "yahoo"
    hours = _market_hours_note()
    if hours:
        print(hours + "\n")
    if today.weekday() != 4:
        print(f"Note: today is {today:%A}; the strategy enters on Fridays "
              "(or the prior trading day if Friday is a holiday).\n")

    rows = read_journal(s["journal"])
    open_ = open_positions(rows, today)
    risk = sum(float(r["max_loss_usd"]) for r in open_)
    if any(date.fromisoformat(r["entry_date"]).isocalendar()[:2] == today.isocalendar()[:2] for r in rows):
        print("Note: you already recorded an entry this week.\n")

    spot, exps, chains = (schwab_chain if src == "schwab" else yahoo_chain)(s, today)
    print(f"{s['symbol']} {spot:.2f}   source: {src}   {datetime.now():%Y-%m-%d %H:%M}")
    if not chains:
        near = sorted(exps, key=lambda e: abs((e - today).days - s["target_dte"]))[:3]
        print(f"SKIP: no expiration within {s['target_dte']} +/- {s['dte_tolerance']} DTE "
              f"(closest: {', '.join(f'{e} ({(e - today).days}d)' for e in near)})")
        return 1
    # closest-to-target expiration first; fall back to the next one when the
    # strikes don't fit (e.g. monthly XSP lists $5 strikes far out, weeklies $1)
    plan, skipped = None, []
    for exp, quotes in chains.items():
        plan, why = build_plan(quotes, exp, today, s)
        if plan is not None:
            break
        skipped.append((exp, why))
    for exp, why in skipped:
        print(f"{'SKIP' if plan is None else 'Passed over'} {exp:%a %b %d, %Y} "
              f"({(exp - today).days} DTE): {why}")
    if plan is None:
        print("SKIP: no expiration in the window fits the strategy this week")
        return 1
    if skipped:
        print()

    print(f"Expiration: {plan.expiration:%a %b %d, %Y} ({plan.dte} DTE)\n")
    names = ("BUY  1  (~20-delta)", "SELL 1  (L1 - %g)" % s["debit_width"],
             "SELL 1  (~10-delta)", "BUY  1  (L3 - %g)" % s["credit_width"])
    print("  Leg                  Strike    Delta    Bid    Ask    Mid")
    for name, q in zip(names, plan.quotes):
        print(f"  {name:<20} {q.strike:>6g}  {q.delta:>7.3f}  {q.bid:>5.2f}  {q.ask:>5.2f}  {q.mid:>5.2f}")
    print(f"\n  Credit at mid {plan.mid_credit:.3f}   (worst fill {plan.natural_credit:.2f}, "
          f"best fill {plan.best_credit:.2f}; negative = debit)")
    print(f"  Short strike {100 * (1 - plan.strikes[2] / spot):.1f}% below {s['symbol']}")
    for k, q in (plan.suspects or {}).items():
        est = next(x for x in plan.quotes if x.strike == k)
        print(f"\n  WARNING: the {k:g} put quote from {src} (bid {q.bid:.2f} / ask {q.ask:.2f}, mid {q.mid:.2f}) "
              f"is out of line with nearby strikes, probably stale.\n"
              f"  Using an estimate of {est.mid:.2f} from the neighboring strikes. "
              f"CHECK the {k:g} put in thinkorswim before trading.")

    if plan.mid_credit < s["min_credit"]:
        print(f"\nSKIP: mid credit {plan.mid_credit:.2f} is below min_credit {s['min_credit']:.2f}")
        return 1
    m = money(plan.mid_credit, s)
    new_risk = risk + m["max_loss"]
    print(f"\n  At the mid: net credit ${m['net_credit']:.2f} after ${m['fees']:.2f} fees, "
          f"max profit ${m['max_profit']:.0f}, max loss ${m['max_loss']:.0f}")
    print(f"  Open positions: {len(open_)} -> {len(open_) + 1} (limit {s['max_open_positions']});  "
          f"total max loss ${risk:,.0f} -> ${new_risk:,.0f} (limit ${s['max_total_risk']:,.0f})")
    if len(open_) + 1 > s["max_open_positions"] or new_risk > s["max_total_risk"]:
        print("\nSKIP: this entry would exceed max_open_positions or max_total_risk")
        return 1

    ladder = limit_ladder(plan.mid_credit, s["min_credit"], s["ladder_steps"])
    print("\nOrder for thinkorswim (Day, LIMIT):")
    print(f"  {tos_ticket(plan, s, ladder[0])}")
    print("  Start at " + ", then ".join(f"{p:.2f}" for p in ladder)
          + " credit, a few minutes each. Don't go below "
          f"{ladder[-1]:.2f}. If thinkorswim's mid differs from {plan.mid_credit:.3f}, start at its mid.")
    l1, l2, l3, l4 = (f"{k:g}" for k in plan.strikes)
    print("\nAfter it fills, record it (replace FILL with your fill price):")
    print(f"  python -m condor_bt.weekly record {plan.expiration} {l1}/{l2}/{l3}/{l4} FILL --mid {plan.mid_credit:.2f}")
    return 0


def cmd_record(a, s):
    strikes = [float(x) for x in a.strikes.split("/")]
    if len(strikes) != 4 or not (strikes[0] > strikes[1] > strikes[2] > strikes[3]):
        sys.exit("strikes must be L1/L2/L3/L4, highest first, e.g. 732/729/690/680")
    d_w, c_w = strikes[0] - strikes[1], strikes[2] - strikes[3]
    s2 = dict(s, debit_width=d_w, credit_width=c_w, contracts=a.contracts)
    m = money(a.fill, s2)
    row = {"entry_date": a.date or date.today().isoformat(), "expiration": a.expiration,
           "l1": f"{strikes[0]:g}", "l2": f"{strikes[1]:g}", "l3": f"{strikes[2]:g}", "l4": f"{strikes[3]:g}",
           "contracts": a.contracts, "fill_credit": f"{a.fill:.2f}",
           "mid_credit": "" if a.mid is None else f"{a.mid:.2f}",
           "fees_usd": f"{m['fees']:.2f}", "max_loss_usd": f"{m['max_loss']:.2f}",
           "max_profit_usd": f"{m['max_profit']:.2f}", "note": a.note or ""}
    append_journal(s["journal"], row)
    print(f"recorded: {row['expiration']} {a.strikes} x{a.contracts} @ {a.fill:.2f} credit "
          f"(max loss ${m['max_loss']:.0f}) -> {s['journal']}")
    return 0


def cmd_status(a, s):
    today = date.today()
    rows = read_journal(s["journal"])
    if not rows:
        print(f"no entries yet in {s['journal']}")
        return 0
    open_ = open_positions(rows, today)
    print(f"{'entry':<11} {'expires':<11} {'strikes':<17} {'fill':>5} {'mid':>5} {'max loss':>9}  status")
    for r in rows:
        exp = date.fromisoformat(r["expiration"])
        st = f"open ({(exp - today).days}d)" if exp >= today else "expired"
        print(f"{r['entry_date']:<11} {r['expiration']:<11} {r['l1']}/{r['l2']}/{r['l3']}/{r['l4']:<5} "
              f"{r['fill_credit']:>5} {r['mid_credit'] or '-':>5} {float(r['max_loss_usd']):>9,.0f}  {st}")
    risk = sum(float(r["max_loss_usd"]) for r in open_)
    print(f"\nopen: {len(open_)} of {s['max_open_positions']}   total max loss ${risk:,.0f} "
          f"of ${s['max_total_risk']:,.0f}")
    slips = [float(r["mid_credit"]) - float(r["fill_credit"]) for r in rows if r.get("mid_credit")]
    if slips:
        avg = sum(slips) / len(slips)
        print(f"fills vs mid: average {avg:+.3f} below mid over {len(slips)} fills "
              f"(backtest: --set pricing=slippage --set slippage_per_leg={max(avg, 0) / 4:.3f})")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m condor_bt.weekly", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--settings", default=os.path.join("configs", "weekly_xsp.yaml"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", help="show this week's order (never sends anything)")
    p.add_argument("--source", choices=["auto", "yahoo", "schwab"])
    r = sub.add_parser("record", help="log a filled order")
    r.add_argument("expiration", help="YYYY-MM-DD")
    r.add_argument("strikes", help="L1/L2/L3/L4, e.g. 732/729/690/680")
    r.add_argument("fill", type=float, help="fill price as a positive credit, e.g. 0.15")
    r.add_argument("--mid", type=float, help="mid credit when you placed it (for slippage tracking)")
    r.add_argument("--contracts", type=int, default=1)
    r.add_argument("--date", help="entry date if not today, YYYY-MM-DD")
    r.add_argument("--note")
    sub.add_parser("status", help="open positions, total risk, fills vs mid")
    sub.add_parser("login", help="(re)do the Schwab API login")
    sub.add_parser("diagnose", help="print environment details for troubleshooting")
    a = ap.parse_args(argv)
    s = load_settings(a.settings)
    return {"plan": cmd_plan, "record": cmd_record, "status": cmd_status, "login": schwab_login,
            "diagnose": cmd_diagnose}[a.cmd](a, s)


if __name__ == "__main__":
    sys.exit(main())
