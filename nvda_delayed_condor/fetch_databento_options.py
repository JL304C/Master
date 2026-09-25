"""
Real-option-price check of the bot's current strategy (put credit spread every cycle,
no call side) using Databento OPRA quotes -- run on your laptop.

For each cycle the bot would have traded since OPRA.PILLAR data begins (Mar 2023):
  1. On the entry day (last trading day of the week, flat) it downloads the real 3:45 PM ET
     bid/ask quotes for the candidate NVDA puts at the ~45-DTE expiry (earnings avoided),
     and picks strikes exactly like the bot: short put = highest strike at or below
     price x 0.95 x 0.98 with delta <= 0.30 (delta from each option's own implied vol),
     long put one width lower. Skips if the MID credit < 10% of the width. Assumes the
     order fills at mid - $0.05 (the bot's limit price).
  2. If NVDA's daily low touches the short strike before expiry, it downloads that day's
     3:45 PM quotes and closes the spread the way the bot does -- a market order: pay the
     short put's ask, receive the long put's bid.
  3. Otherwise the spread settles at expiry from NVDA's close.

Width: $20 today (NVDA ~$225) is ~9% of the price. Earlier trades use the same ~9% (so
~$40 wide before the June 2024 10:1 split) and every P&L is converted to "per $20-wide
spread" so all trades are comparable with today's bot.

Needs: DATABENTO_API_KEY in .env, nvda_daily_databento.csv (from fetch_databento_daily.py),
nvda_earnings_dates.txt and condor_rules.py next to this script.

Run:  python fetch_databento_options.py
It first adds up Databento's cost estimate for every download it plans and only continues
after you type y. Downloads are cached in opra_cache\\ so re-runs cost nothing.
Output: nvda_real_option_trades.csv (one row per trade) + a summary printed at the end.
"""
from __future__ import annotations

import csv
import hashlib
import math
import os
import sys
import warnings
from datetime import date, datetime, time, timedelta
from pathlib import Path

import databento as db
import pandas as pd

import condor_rules as rules

HERE = Path(__file__).resolve().parent
DATASET = "OPRA.PILLAR"
SCHEMA = "cbbo-1m"                 # consolidated best bid/offer, 1-minute snapshots
CACHE = HERE / "opra_cache"
OUT = HERE / "nvda_real_option_trades.csv"

# ---- the bot's settings (nvda_condor_bot.py) ----
TODAY_PRICE = 224.58
WIDTH_TODAY = 20.0
WIDTH_FRAC = WIDTH_TODAY / TODAY_PRICE      # ~8.9% of the price
EVERY_CYCLE_OTM = 0.05
SUPPORT_BUFFER = 0.98
MAX_PUT_DELTA = 0.30
MIN_PUT_CREDIT_PCT = 0.10
ENTRY_CONCESSION_TODAY = 0.05               # $ under mid, scaled with the width
FEE_PER_CONTRACT = 0.03
R = 0.04
QUOTE_TIME = time(15, 45)                   # the bot runs at 3:45 PM ET
SPLITS = [(date(2024, 6, 10), 10)]          # (effective date, ratio) after OPRA.PILLAR starts


# --------------------------------------------------------------------------- #
def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def split_factor(d: date) -> int:
    """Real (as-traded) price = split-adjusted price x this factor on date d."""
    f = 1
    for eff, ratio in SPLITS:
        if d < eff:
            f *= ratio
    return f


def load_daily():
    rows = []
    with open(Path(os.environ.get('NVDA_DAILY_CSV', HERE / 'nvda_daily_databento.csv'))) as f:
        for r in csv.DictReader(f):
            rows.append(dict(d=date.fromisoformat(r["date"]), o=float(r["open"]), h=float(r["high"]),
                             l=float(r["low"]), c=float(r["close"])))
    return sorted(rows, key=lambda x: x["d"])


def ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_put(S, K, T, sig):
    d1 = (math.log(S / K) + (R + 0.5 * sig * sig) * T) / (sig * math.sqrt(T))
    d2 = d1 - sig * math.sqrt(T)
    return K * math.exp(-R * T) * ncdf(-d2) - S * ncdf(-d1)


def put_delta_from_price(S, K, T, price):
    """Delta of a put from its own market price (implied vol by bisection)."""
    intrinsic = max(0.0, K * math.exp(-R * T) - S)
    if price <= intrinsic + 1e-4 or T <= 0:
        return None
    lo, hi = 1e-3, 5.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if bs_put(S, K, T, mid) > price:
            hi = mid
        else:
            lo = mid
    sig = (lo + hi) / 2
    d1 = (math.log(S / K) + (R + 0.5 * sig * sig) * T) / (sig * math.sqrt(T))
    return ncdf(d1) - 1


def occ(expiry: date, strike: float) -> str:
    """OPRA raw symbol, e.g. 'NVDA  261106P00205000' (root padded to 6 characters)."""
    return f"{'NVDA':<6}{expiry:%y%m%d}P{int(round(strike * 1000)):08d}"


def et_window(d: date):
    t1 = pd.Timestamp(datetime.combine(d, QUOTE_TIME), tz="America/New_York")
    return (t1 - pd.Timedelta(minutes=10)).tz_convert("UTC"), (t1 + pd.Timedelta(minutes=1)).tz_convert("UTC")


# --------------------------------------------------------------------------- #
class Quotes:
    """Cached Databento quote downloads: {symbol: (bid, ask)} at ~3:45 PM ET on a day."""

    def __init__(self, client):
        self.client = client
        CACHE.mkdir(exist_ok=True)

    def _path(self, d, symbols):
        key = hashlib.sha1((d.isoformat() + "|" + ",".join(sorted(symbols))).encode()).hexdigest()[:16]
        return CACHE / f"{d.isoformat()}_{key}.dbn.zst"

    def cost(self, d, symbols) -> float:
        if self._path(d, symbols).exists():
            return 0.0
        s, e = et_window(d)
        return self.client.metadata.get_cost(dataset=DATASET, symbols=symbols, schema=SCHEMA,
                                             stype_in="raw_symbol", start=s, end=e)

    def get(self, d, symbols) -> dict:
        path = self._path(d, symbols)
        if path.exists():
            store = db.DBNStore.from_file(path)
        else:
            s, e = et_window(d)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")      # strikes that were never listed just come back empty
                store = self.client.timeseries.get_range(dataset=DATASET, symbols=symbols, schema=SCHEMA,
                                                         stype_in="raw_symbol", start=s, end=e, path=path)
        df = store.to_df()
        out = {}
        if len(df):
            df = df.sort_index()
            for sym, g in df.groupby("symbol"):
                last = g.iloc[-1]
                bid, ask = float(last["bid_px_00"]), float(last["ask_px_00"])
                if bid > 0 and ask > 0 and ask >= bid:
                    out[sym.strip() if isinstance(sym, str) else sym] = (bid, ask)
        return out


# --------------------------------------------------------------------------- #
def week_end_days(D):
    """Index of the last trading day of every week (the bot's entry days)."""
    last = {}
    for i, x in enumerate(D):
        last[x["d"].isocalendar()[:2]] = i
    return sorted(last.values())


def pick_expiry(D, i, earn):
    d0 = D[i]["d"]
    best = None
    for dte in range(28, 53):
        f = d0 + timedelta(days=dte)
        if f.weekday() != 4 or rules.earnings_between(earn, d0, f):
            continue
        if best is None or abs(dte - 45) < abs(best[1] - 45):
            best = (f, dte)
    if best is None or best[0] > D[-1]["d"]:
        return None
    f, dte = best
    j = i
    while j + 1 < len(D) and D[j + 1]["d"] <= f:
        j += 1
    return j, f, dte                      # (last trading day index, contract expiry date, DTE)


def entry_candidates(S_real, exp, width_real):
    ref = S_real * (1 - EVERY_CYCLE_OTM) * SUPPORT_BUFFER
    top = math.floor(ref / 5) * 5
    shorts = [top - 5 * k for k in range(0, int(0.20 * S_real / 5) + 1)]
    strikes = sorted(set(shorts) | {k - width_real for k in shorts})
    return ref, shorts, {k: occ(exp, k) for k in strikes if k > 0}


def simulate(D, earn, quotes: Quotes | None, first_day: date, estimate_only=False):
    week_ends = week_end_days(D)
    trades, est_cost, busy_until = [], 0.0, -1
    for i in week_ends:
        if D[i]["d"] < first_day or i <= busy_until or i >= len(D) - 1:
            continue
        ex = pick_expiry(D, i, earn)
        if ex is None:
            continue
        ie, exp, dte = ex
        f = split_factor(D[i]["d"])
        S = D[i]["c"] * f                                     # real price that day
        width = max(5.0, round(WIDTH_FRAC * S / 5) * 5)
        norm = WIDTH_TODAY / width                            # -> per $20-wide spread
        ref, shorts, syms = entry_candidates(S, exp, width)
        if estimate_only:
            est_cost += quotes.cost(D[i]["d"], list(syms.values()))
            busy_until = ie
            continue
        q = quotes.get(D[i]["d"], list(syms.values()))
        T = dte / 365
        pick = None
        for K in shorts:                                      # highest first
            qs, ql = q.get(syms.get(K)), q.get(syms.get(K - width))
            if not qs or not ql:
                continue
            mid_s = (qs[0] + qs[1]) / 2
            dl = put_delta_from_price(S, K, T, mid_s)
            if dl is None or -dl > MAX_PUT_DELTA:
                continue
            pick = (K, qs, ql, dl)
            break
        if pick is None:
            trades.append(dict(entry=D[i]["d"], exp=exp, status="no_quotes"))
            continue
        K, qs, ql, dl = pick
        mid_credit = (qs[0] + qs[1]) / 2 - (ql[0] + ql[1]) / 2
        if mid_credit < MIN_PUT_CREDIT_PCT * width:
            trades.append(dict(entry=D[i]["d"], exp=exp, status="skipped_low_credit",
                               mid_credit=round(mid_credit * norm, 2)))
            continue                                          # like the bot: try again next week
        fill = mid_credit - ENTRY_CONCESSION_TODAY / norm
        t = dict(entry=D[i]["d"], exp=exp, dte=dte, price=round(S, 2), width=width, short=K, long=K - width,
                 short_delta=round(dl, 3), bid_ask_short=qs, bid_ask_long=ql,
                 mid_credit=mid_credit, fill_credit=fill, status="expiry", exit_day=exp, close_cost=None)
        # daily monitoring: touch of the short strike. Prices below are in entry-day units
        # (daily bars x f), so they compare directly with K even across a split.
        for j in range(i + 1, ie + 1):
            if j < ie and D[j]["l"] * f <= K:
                qx = quotes.get(D[j]["d"], [syms[K], syms[K - width]])
                a, b = qx.get(syms[K]), qx.get(syms[K - width])
                if a and b:
                    cost = min(width, a[1] - b[0])
                    t["status"] = "touch_close"
                else:
                    Sx = D[j]["c"] * f             # no quotes (e.g. symbol changed at a split):
                    cost = min(width, max(0.0, K - Sx) + 0.10 * width)
                    t["status"] = "touch_close_no_quote"
                t["close_cost"], t["exit_day"] = cost, D[j]["d"]
                break
        if t["close_cost"] is None:
            S_exp = D[ie]["c"] * f
            t["close_cost"] = min(width, max(0.0, K - S_exp))
        fees = FEE_PER_CONTRACT * (4 if t["status"].startswith("touch") else 2)
        t["pnl_per_20wide"] = round(((fill - t["close_cost"]) * 100 - fees) * norm, 2)
        t["credit_per_20wide"] = round(fill * norm, 2)
        trades.append(t)
        busy_until = ie
    return trades, est_cost


def summarize(trades):
    done = [t for t in trades if "pnl_per_20wide" in t]
    if not done:
        print("No completed trades.")
        return
    p = [t["pnl_per_20wide"] for t in done]
    eq = pk = dd = 0.0
    for x in p:
        eq += x; pk = max(pk, eq); dd = max(dd, pk - eq)
    years = (done[-1]["exp"] - done[0]["entry"]).days / 365.25
    print(f"\nREAL option prices, per $20-wide spread, 1 contract, {done[0]['entry']} .. {done[-1]['exp']}:")
    print(f"  trades {len(p)}   won {sum(x > 0 for x in p) / len(p):.0%}   total ${sum(p):,.0f}   "
          f"avg ${sum(p) / len(p):,.0f}   worst ${min(p):,.0f}   max drawdown ${dd:,.0f}   (~${sum(p) / years:,.0f}/yr)")
    print(f"  avg credit received ${sum(t['credit_per_20wide'] for t in done) / len(done):.2f}   "
          f"touch-closes {sum(t['status'].startswith('touch') for t in done)}   "
          f"skipped (credit < 10%) {sum(t.get('status') == 'skipped_low_credit' for t in trades)}   "
          f"no quotes {sum(t.get('status') == 'no_quotes' for t in trades)}")


def main():
    load_env_file(HERE / ".env")
    key = os.environ.get("DATABENTO_API_KEY")
    if not key:
        print("Missing DATABENTO_API_KEY (add it to the .env file next to this script).")
        sys.exit(1)
    client = db.Historical(key)
    D = load_daily()
    earn = rules.load_earnings(HERE / "nvda_earnings_dates.txt")
    rng = client.metadata.get_dataset_range(dataset=DATASET)
    first = date.fromisoformat(rng["start"][:10]) + timedelta(days=1)
    print(f"{DATASET} starts {rng['start'][:10]}; testing entries from {first} to {D[-1]['d']}")

    quotes = Quotes(client)
    _, est = simulate(D, earn, quotes, first, estimate_only=True)
    print(f"Estimated Databento cost: about ${est:,.2f} for the entry-day quotes, plus a few cents per "
          f"touch-day download (cached files are free).")
    if input("Download? [y/N] ").strip().lower() != "y":
        print("Cancelled, nothing downloaded.")
        return

    trades, _ = simulate(D, earn, quotes, first)
    cols = ["entry", "exp", "status", "dte", "price", "width", "short", "long", "short_delta",
            "bid_ask_short", "bid_ask_long", "mid_credit", "fill_credit", "credit_per_20wide",
            "exit_day", "close_cost", "pnl_per_20wide"]
    with open(OUT, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for t in trades:
            w.writerow(t)
    print(f"Saved {len(trades)} rows to {OUT.name}")
    summarize(trades)


if __name__ == "__main__":
    main()
