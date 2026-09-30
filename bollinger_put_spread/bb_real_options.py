"""
Real-option-price check of the Bollinger put spread (variant A+B, what the bot trades now)
against a no-signal baseline, using Databento OPRA quotes -- run on your laptop.

The stock-side backtest priced options with a model. This replays the same rules with
REAL bid/ask quotes:

  signal   (A+B, as the bot)  close back above the lower band after an oversold close;
           next morning pick the nearest standard monthly 45-90 DTE before earnings from
           the REAL listed chain; short = highest listed strike below min(POC, band) x 0.95;
           long = listed strike nearest short - 1% of the close; skip if the mid credit
           < $0.50; assume the order fills at the mid (the bot's limit price).
  baseline (no signal)        the same, entered on the last trading day of each week when
           no spread is open, short strike below the close x (1 - 8.4%) (the typical
           distance of the A+B trades).
  exits    every day at ~3:45 PM ET with real quotes, the bot's rules (bb_rules.exit_reason):
           2x stop, close below the short strike, 50% take profit, 21 DTE. The exit PAYS the
           natural price (short ask - long bid), like the bot's closing order; P&L is also
           shown at the mid, the optimistic case. $0.03 per contract per leg in fees.

Data: Alpaca daily bars (split-adjusted for the signal, raw for strikes), Alpha Vantage
earnings (uses bb_stock_backtest's cached files when present), Databento OPRA.PILLAR
cbbo-1m quotes:
  - the entry day: every put on the ticker (parent symbol) in a 3-minute window at 9:45 AM;
  - while a spread is open: just its two contracts, one request per trade.
Downloads are cached in opra_cache\\ so re-runs cost nothing. It prints Databento's cost
estimate first and only continues after you type y.

Needs: pip install databento pandas alpaca-py ; DATABENTO_API_KEY (and the Alpaca and
Alpha Vantage keys) in the .env next to this script.
Run:     python bb_real_options.py
Options: --tickers AMD,MSFT   (default: the bot's watchlist)   --only signal|baseline
         --start 2016-01-01   --yes (skip the cost prompt)
Output:  bb_real_option_trades.csv + summary tables.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import statistics
import sys
import warnings
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import bb_rules as rules
from bb_stock_backtest import arg, earnings_from_alpha_vantage, load_env

HERE = Path(__file__).resolve().parent
CACHE = HERE / "opra_cache"
OUT = HERE / "bb_real_option_trades.csv"
DATASET = "OPRA.PILLAR"
SCHEMA = "cbbo-1m"
ENTRY_TIME = time(9, 45)          # the bot's --enter run
CHECK_TIME = time(15, 45)         # the bot's --manage run
MIN_CREDIT = 0.50
STRIKE_OFFSET = 0.05              # B
DTE_MIN, DTE_MAX = 45, 90
BASELINE_OTM = 0.084              # typical A+B short-strike distance in the stock-side backtest
FEE_PER_LEG = 0.03
DOWNLOAD_TIMEOUT = 180            # seconds; a stuck Databento download is abandoned, retried once, then skipped
DEFAULT_TICKERS = ["AMD", "MSFT", "GOOGL", "META", "AMZN", "UNH", "CAT", "COST", "SPY"]

OSI_TAIL = re.compile(r"(\d{6})([CP])(\d{8})$")


def parse_osi(sym: str):
    """(expiry, right, strike) from an OSI symbol however it's padded, e.g.
    'AMD   261120P00400000' -> (2026-11-20, 'P', 400.0). None if it doesn't parse."""
    m = OSI_TAIL.search(str(sym).replace(" ", ""))
    if not m:
        return None
    return datetime.strptime(m.group(1), "%y%m%d").date(), m.group(2), int(m.group(3)) / 1000.0


# --------------------------------------------------------------------------- #
# quotes
# --------------------------------------------------------------------------- #
def et_window(d: date, at: time, minutes_before=2, minutes_after=1):
    import pandas as pd
    t = pd.Timestamp(datetime.combine(d, at), tz="America/New_York")
    return (t - pd.Timedelta(minutes=minutes_before)).tz_convert("UTC"), (t + pd.Timedelta(minutes=minutes_after)).tz_convert("UTC")


def parse_chain_df(df) -> dict:
    """Databento cbbo rows -> {(expiry, strike): (bid, ask, raw_symbol)} for puts, using each
    contract's last quote in the window."""
    out = {}
    if df is None or not len(df):
        return out
    df = df.sort_index()
    for sym, g in df.groupby("symbol"):
        p = parse_osi(sym)
        if not p or p[1] != "P":
            continue
        last = g.iloc[-1]
        bid, ask = float(last["bid_px_00"]), float(last["ask_px_00"])
        if bid > 0 and ask > 0 and ask >= bid:
            out[(p[0], p[2])] = (bid, ask, sym)
    return out


def parse_window_df(df, at: time = CHECK_TIME) -> dict:
    """Databento cbbo rows over several days -> {date: {raw_symbol: (bid, ask)}}, each the last
    two-sided quote at or before `at` ET that day (so a 1 PM early close uses ~12:59)."""
    out = {}
    if df is None or not len(df):
        return out
    et = df.copy()
    et.index = et.index.tz_convert("America/New_York")
    et = et.sort_index()
    et = et[(et["bid_px_00"] > 0) & (et["ask_px_00"] > 0) & (et["ask_px_00"] >= et["bid_px_00"])]
    et = et[[t <= at for t in et.index.time]]
    for (d, sym), g in et.groupby([et.index.date, "symbol"]):
        last = g.iloc[-1]
        out.setdefault(d, {})[sym] = (float(last["bid_px_00"]), float(last["ask_px_00"]))
    return out


# Option roots that changed: before the date, the options traded under the old root.
ROOT_CHANGES = {"META": [(date(2022, 6, 9), "FB")]}


def option_root(ticker: str, d: date) -> str:
    for changed_on, old in ROOT_CHANGES.get(ticker, []):
        if d < changed_on:
            return old
    return ticker


def root_changes_between(ticker: str, d0: date, d1: date) -> bool:
    return any(d0 < changed_on <= d1 for changed_on, _ in ROOT_CHANGES.get(ticker, []))


class DatabentoQuotes:
    def __init__(self, client, offline=False):
        self.client = client
        self.offline = offline                               # True: use the cache only, never download
        CACHE.mkdir(exist_ok=True)

    def _chain_path(self, ticker, d):
        return CACHE / f"{option_root(ticker, d)}_puts_{d.isoformat()}_{ENTRY_TIME:%H%M}.dbn.zst"

    def chain_cost(self, ticker, d) -> float:
        if self._chain_path(ticker, d).exists():
            return 0.0
        s, e = et_window(d, ENTRY_TIME)
        try:
            return self.client.metadata.get_cost(dataset=DATASET, symbols=[f"{option_root(ticker, d)}.OPT"],
                                                 schema=SCHEMA, stype_in="parent", start=s, end=e)
        except Exception as exc:                          # noqa: BLE001 -- estimate only
            print(f"    (no cost estimate for {ticker} on {d}: {str(exc).splitlines()[0]})")
            return 0.0

    def _load(self, path, **req):
        """Cached download. Writes to a .part file and renames it only when complete, so an
        interrupted run (Ctrl+C, sleep, dropped connection) never leaves a half file that a
        re-run would trust; a cached file that won't read is deleted and fetched again. A
        download that takes longer than DOWNLOAD_TIMEOUT is abandoned and retried once."""
        import databento as db
        if path.exists():
            try:
                return db.DBNStore.from_file(path).to_df()
            except Exception:                             # noqa: BLE001 -- damaged/partial cache file
                print(f"  (re-downloading damaged cache file {path.name})")
                path.unlink()
        if self.offline:
            raise RuntimeError("not in the cache (offline)")
        last_err = None
        for attempt in (1, 2):
            part = path.with_name(path.name + f".part{attempt}")
            part.unlink(missing_ok=True)
            err = self._download(part, req)
            if err is None:
                part.replace(path)
                return db.DBNStore.from_file(path).to_df()
            last_err = err
            print(f"  (download attempt {attempt} failed: {err})", flush=True)
        raise RuntimeError(last_err)

    def _download(self, part, req):
        """Run get_range in a background thread with a wall-clock limit. Returns None or an error."""
        import threading
        box = {}

        def work():
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")           # "no data" / degraded-day notices
                    self.client.timeseries.get_range(dataset=DATASET, schema=SCHEMA, path=part, **req)
                box["ok"] = True
            except Exception as exc:                          # noqa: BLE001
                box["err"] = str(exc).splitlines()[0] if str(exc) else type(exc).__name__

        th = threading.Thread(target=work, daemon=True)       # daemon: a stuck socket can't block exit
        th.start()
        th.join(DOWNLOAD_TIMEOUT)
        if th.is_alive():
            return f"no response after {DOWNLOAD_TIMEOUT}s"
        if "ok" in box and part.exists():
            return None
        return box.get("err", "download produced no file")

    def chain(self, ticker, d) -> dict:
        s, e = et_window(d, ENTRY_TIME)
        try:
            return parse_chain_df(self._load(self._chain_path(ticker, d), symbols=[f"{option_root(ticker, d)}.OPT"],
                                             stype_in="parent", start=s, end=e))
        except Exception as exc:                          # noqa: BLE001 -- that morning counts as "no quotes"
            if not self.offline:
                print(f"  {ticker} {d}: no option data ({str(exc).splitlines()[0]})")
            return {}

    def window(self, symbols, d0, d1) -> dict:
        key = hashlib.sha1(f"{sorted(symbols)}|{d0}|{d1}".encode()).hexdigest()[:16]
        path = CACHE / f"window_{key}.dbn.zst"
        s = et_window(d0, time(9, 30), 0, 0)[0]
        e = et_window(d1, time(16, 0), 0, 0)[0]
        try:
            return parse_window_df(self._load(path, symbols=list(symbols), stype_in="raw_symbol", start=s, end=e))
        except Exception as exc:                          # noqa: BLE001
            if not self.offline:
                print(f"  {symbols} {d0}..{d1}: no option data ({str(exc).splitlines()[0]})")
            return None


# --------------------------------------------------------------------------- #
# data
# --------------------------------------------------------------------------- #
def alpaca_bars(symbol, start):
    """(split-adjusted bars for the signal, raw closes/opens by date for strikes)."""
    import os
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    from alpaca.data.enums import Adjustment, DataFeed
    client = StockHistoricalDataClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])
    out = {}
    for adj in (Adjustment.SPLIT, Adjustment.RAW):
        req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day,
                               start=datetime.combine(start, datetime.min.time(), timezone.utc),
                               end=datetime.now(timezone.utc) - timedelta(minutes=16),
                               adjustment=adj, feed=DataFeed.SIP)
        out[adj] = [dict(d=b.timestamp.date(), o=float(b.open), h=float(b.high), l=float(b.low),
                         c=float(b.close), v=float(b.volume)) for b in client.get_stock_bars(req)[symbol]]
    raw = {b["d"]: b for b in out[Adjustment.RAW]}
    split = [b for b in out[Adjustment.SPLIT] if b["d"] in raw]
    return split, raw


def load_earnings(symbol):
    """Historical report dates don't change: reuse bb_stock_backtest's cache whatever its age."""
    cache = HERE / f"bb_backtest_earnings_{symbol}.json"
    if cache.exists():
        return [date.fromisoformat(d) for d in json.loads(cache.read_text())["dates"]]
    return earnings_from_alpha_vantage(symbol)


def last_day_of_week(bars, i):
    return i + 1 >= len(bars) or bars[i + 1]["d"].isocalendar()[:2] != bars[i]["d"].isocalendar()[:2]


# --------------------------------------------------------------------------- #
# replay
# --------------------------------------------------------------------------- #
def monthly_possible(ed: date, nxt: date | None) -> bool:
    """Could any standard monthly 45-90 DTE expire before the next earnings? (3rd Friday, or the
    Thursday before it on a holiday.) Checked before paying for the morning's chain."""
    y, m = ed.year, ed.month
    for _ in range(5):
        tf = rules.third_friday(y, m)
        for exp in (tf, tf - timedelta(days=1)):
            if DTE_MIN <= (exp - ed).days <= DTE_MAX and (nxt is None or exp < nxt):
                return True
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return False


def entry_setups(ticker, bars, raw, earnings, mode, first_day):
    """Generator of entry attempts (i, caps) in date order; the caller decides whether it is
    flat. caps = (poc cap, band cap) in RAW price units."""
    start = max(rules.BB_PERIOD + 1, rules.POC_LOOKBACK)
    for i in range(start, len(bars) - 1):
        if bars[i]["d"] < first_day:
            continue
        nxt = next((x for x in earnings if x >= bars[i + 1]["d"]), None)
        if not monthly_possible(bars[i + 1]["d"], nxt):
            if mode == "signal" and rules.cross_above_lower(bars, i):
                yield i, None, {}                             # counted as skipped, nothing downloaded
            continue
        f = raw[bars[i]["d"]]["c"] / bars[i]["c"]             # split factor that day
        if mode == "signal":
            hit = rules.cross_above_lower(bars, i)
            if not hit:
                continue
            poc = rules.volume_profile_poc(bars[:i + 1])
            yield i, (poc * (1 - STRIKE_OFFSET) * f, hit["lower"] * (1 - STRIKE_OFFSET) * f), \
                dict(poc=round(poc * f, 2), lower=round(hit["lower"] * f, 2))
        elif last_day_of_week(bars, i):
            cap = raw[bars[i]["d"]]["c"] * (1 - BASELINE_OTM)
            yield i, (cap, cap), {}


def replay(ticker, bars, raw, earnings, mode, quotes, first_day, log, on_window=None):
    trades, skips, busy_until = [], {}, -1
    n = len(bars)

    def skip(why):
        skips[why] = skips.get(why, 0) + 1

    for i, caps, extra in entry_setups(ticker, bars, raw, earnings, mode, first_day):
        if i < busy_until:
            if mode == "signal":
                skip("spread already open")
            continue
        if caps is None:
            skip("no monthly before earnings")
            continue
        cap_poc, cap_band = caps
        e = i + 1
        ed = bars[e]["d"]
        close_i = raw[bars[i]["d"]]["c"]
        nxt = next((x for x in earnings if x >= ed), None)
        chain = quotes.chain(ticker, ed)
        if not chain:
            skip("no quotes that morning")
            continue
        exps = rules.eligible_expirations(sorted({x for x, _ in chain}), ed, nxt, DTE_MIN, DTE_MAX, True)
        if not exps:
            skip("no monthly before earnings")
            continue
        exp = exps[0]
        strikes = sorted(k for x, k in chain if x == exp)
        picked = rules.pick_strikes(strikes, cap_poc, cap_band, rules.target_width(close_i))
        if not picked:
            skip("no strikes")
            continue
        short, long = picked
        bs, as_, sym_s = chain[(exp, short)]
        bl, al, sym_l = chain[(exp, long)]
        credit = round((bs + as_) / 2 - (bl + al) / 2, 2)
        S0 = raw[ed]["o"]
        t = dict(ticker=ticker, mode=mode, signal=bars[i]["d"], entry=ed, expiration=exp, price=round(S0, 2),
                 short=short, long=long, width=short - long, otm_pct=round(1 - short / S0, 4), credit=credit,
                 short_quote=(bs, as_), long_quote=(bl, al), **extra)
        if credit < MIN_CREDIT:
            skip("credit < $0.50")
            continue
        # hold from the entry day's 3:45 PM check to the 21-DTE day (or the last bar)
        end = e
        while end + 1 < n and (exp - bars[end]["d"]).days > rules.TIME_STOP_DTE:
            end += 1
        f_entry = raw[ed]["c"] / bars[e]["c"]
        if any(abs(raw[bars[j]["d"]]["c"] / bars[j]["c"] - f_entry) / f_entry > 0.2 for j in range(e, end + 1)):
            skip("stock split during the trade")
            busy_until = end
            continue
        if root_changes_between(ticker, ed, bars[end]["d"]):
            skip("option symbol changed during the trade")
            busy_until = end
            continue
        daily = quotes.window([sym_s, sym_l], ed, bars[end]["d"])
        if daily is None:
            skip("no quotes during the trade")
            busy_until = end
            continue
        if on_window:                                         # bb_exit_study.py scores other exit rules here
            on_window(t, dict(bars=bars, raw=raw, e=e, end=end, exp=exp, short=short, long=long, credit=credit,
                              sym_s=sym_s, sym_l=sym_l, daily=daily))
        exit_j = reason = None
        last_q = None
        for j in range(e, end + 1):
            d = bars[j]["d"]
            q = daily.get(d, {})
            qs, ql = q.get(sym_s), q.get(sym_l)
            val = None
            if qs and ql:
                val = round((qs[0] + qs[1]) / 2 - (ql[0] + ql[1]) / 2, 2)
                last_q = (qs, ql)
            reason = rules.exit_reason(credit, val, raw[d]["c"], short, (exp - d).days)
            if reason:
                exit_j = j
                break
        if exit_j is None:
            t.update(status="still open")
            trades.append(t)
            busy_until = n
            continue
        width = short - long
        if qs and ql:
            natural = min(width, max(0.0, qs[1] - ql[0]))
            mid = min(width, max(0.0, val))
            quoted = "yes"
        elif last_q:                                          # no quote that day: last known + half a spread
            (ps, pl) = last_q
            mid = min(width, max(0.0, (ps[0] + ps[1]) / 2 - (pl[0] + pl[1]) / 2))
            natural = min(width, mid + ((ps[1] - ps[0]) + (pl[1] - pl[0])) / 2)
            quoted = "stale"
        else:
            intrinsic = max(0.0, short - raw[bars[exit_j]["d"]]["c"]) - max(0.0, long - raw[bars[exit_j]["d"]]["c"])
            mid = natural = min(width, intrinsic + 0.10)
            quoted = "none"
        fees = FEE_PER_LEG * 4
        t.update(status="closed", exit=bars[exit_j]["d"], exit_reason=reason, exit_natural=round(natural, 2),
                 exit_mid=round(mid, 2), exit_quoted=quoted, days=(bars[exit_j]["d"] - ed).days,
                 pnl=round((credit - natural) * 100 - fees, 2), pnl_mid=round((credit - mid) * 100 - fees, 2))
        trades.append(t)
        log(f"  {ticker:<6}{mode:<9}{ed} {exp} {short:g}/{long:g}P credit {credit:.2f} -> {reason:<12} "
            f"{bars[exit_j]['d']}  ${t['pnl']:>7,.0f}")
        busy_until = exit_j
    return trades, skips


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #
def stats(trades, key="pnl"):
    p = [t[key] for t in sorted(trades, key=lambda x: x["entry"]) if t.get(key) is not None]
    if not p:
        return None
    eq = pk = dd = 0.0
    for x in p:
        eq += x
        pk = max(pk, eq)
        dd = min(dd, eq - pk)
    sd = statistics.pstdev(p) if len(p) > 1 else 0
    return dict(n=len(p), win=sum(x > 0 for x in p) / len(p), total=sum(p), avg=statistics.mean(p),
                worst=min(p), dd=dd, t=(statistics.mean(p) / (sd / math.sqrt(len(p)))) if sd else 0.0)


def row(label, trades, width=10):
    s, m = stats(trades), stats(trades, "pnl_mid")
    if not s:
        return f"{label:<{width}}{'no closed trades':>20}"
    credits = [t["credit"] for t in trades if t.get("pnl") is not None]
    return (f"{label[:width]:<{width}}{s['n']:>7}{s['win']:>6.0%}{s['total']:>10,.0f}{s['avg']:>7,.0f}{s['worst']:>8,.0f}"
            f"{s['dd']:>9,.0f}{s['t']:>6.1f}{statistics.mean(credits):>8.2f}{m['total']:>13,.0f}")


HEADER = (f"{'':<10}{'trades':>7}{'won':>6}{'total':>10}{'avg':>7}{'worst':>8}{'maxDD':>9}{'t':>6}"
          f"{'credit':>8}{'total@mid':>13}")


def main():
    load_env()
    import os
    key = os.environ.get("DATABENTO_API_KEY")
    if not key:
        sys.exit("Missing DATABENTO_API_KEY -- add it to the .env next to this script "
                 "(the same key as in the NVDA condor folder).")
    import databento as db
    client = db.Historical(key)
    quotes = DatabentoQuotes(client)
    tickers = [x.strip().upper() for x in (arg("--tickers") or ",".join(DEFAULT_TICKERS)).split(",") if x.strip()]
    modes = [arg("--only")] if arg("--only") else ["signal", "baseline"]
    start = date.fromisoformat(arg("--start", "2016-01-01"))
    rng = client.metadata.get_dataset_range(dataset=DATASET)
    first_day = max(date.fromisoformat(rng["start"][:10]) + timedelta(days=1), start)

    data = {}
    for tk in tickers:
        try:
            bars, raw = alpaca_bars(tk, start - timedelta(days=200))
            data[tk] = (bars, raw, load_earnings(tk))
            print(f"{tk}: {len(bars)} daily bars, {len(data[tk][2])} earnings dates")
        except Exception as exc:                          # noqa: BLE001
            print(f"{tk}: SKIPPED -- {exc}")

    # ---- cost estimate: entry-morning chains are the bulk; sample 3 days per ticker ----
    est_total, est_days = 0.0, 0
    for tk, (bars, raw, earn) in data.items():
        days = []
        for mode in modes:
            setups = [x for x in entry_setups(tk, bars, raw, earn, mode, first_day) if x[1] is not None]
            # baseline entries are roughly one per 5 weeks of holding + retries; signal: every setup
            days += [bars[i + 1]["d"] for i, *_ in (setups if mode == "signal" else setups[::3])]
        days = sorted(set(days))
        if not days:
            continue
        sample = [days[0], days[len(days) // 2], days[-1]]
        per = statistics.mean(quotes.chain_cost(tk, d) for d in sample)
        est_total += per * len(days) * 1.15                # +15% for the per-trade quote windows
        est_days += len(days)
        print(f"  {tk}: ~{len(days)} entry mornings x ~${per:,.3f} = ~${per * len(days):,.2f}")
    print(f"Estimated Databento cost: ~${est_total:,.2f} for ~{est_days} entry mornings plus the per-trade quote "
          f"windows. Cached downloads are free on re-runs.")
    if "--yes" not in sys.argv and input("Download? [y/N] ").strip().lower() != "y":
        print("Cancelled, nothing downloaded.")
        return

    print("Replaying with real quotes -- a line prints per closed trade, and a summary row after each ticker.")
    print("(Tip: don't click inside the PowerShell window while it runs -- that pauses the program until you press Enter.)")
    results = {}
    for tk, (bars, raw, earn) in data.items():
        for mode in modes:
            print(f"--- {tk} {mode} ---", flush=True)
            try:
                results[(tk, mode)] = replay(tk, bars, raw, earn, mode, quotes, first_day,
                                             lambda msg: print(msg, flush=True))
                print(HEADER.replace("          ", f"{'':<10}", 1))
                print(row(f"{tk} {mode[:4]}", results[(tk, mode)][0]), flush=True)
            except Exception as exc:                      # noqa: BLE001
                print(f"  {tk} {mode}: FAILED -- {exc}")

    all_trades = [t for tr, _ in results.values() for t in tr]
    cols = ["ticker", "mode", "signal", "entry", "expiration", "price", "short", "long", "width", "otm_pct",
            "credit", "short_quote", "long_quote", "status", "exit", "exit_reason", "exit_natural", "exit_mid",
            "exit_quoted", "days", "pnl", "pnl_mid", "poc", "lower"]
    with OUT.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(all_trades)

    print("\nREAL option prices, 1 contract per trade. Entry at the mid; exits pay the natural price "
          "(total@mid = exits at the mid instead).")
    for mode in modes:
        tr = [t for (tk, m), (ts, _) in results.items() if m == mode for t in ts]
        print(f"\n=== {mode.upper()} ({'A+B, the bot' if mode == 'signal' else 'no signal, weekly, short ~8.4% below'}) ===")
        print(HEADER)
        print(row("POOLED", tr))
        for tk in data:
            if (tk, mode) in results:
                print(row(tk, results[(tk, mode)][0]))
        reasons, skipped = {}, {}
        for t in tr:
            if t.get("exit_reason"):
                reasons[t["exit_reason"]] = reasons.get(t["exit_reason"], 0) + 1
        for (tk, m), (_, sk) in results.items():
            if m == mode:
                for k, v in sk.items():
                    skipped[k] = skipped.get(k, 0) + v
        print("exits: " + ", ".join(f"{k} {v}" for k, v in sorted(reasons.items())))
        print("skipped: " + (", ".join(f"{k} {v}" for k, v in sorted(skipped.items())) or "none"))
        stale = sum(1 for t in tr if t.get("exit_quoted") in ("stale", "none"))
        if stale:
            print(f"exits without a same-day quote (estimated): {stale}")
        years = {}
        for t in tr:
            if t.get("pnl") is not None:
                years[t["entry"].year] = years.get(t["entry"].year, 0) + t["pnl"]
        print("by year: " + ", ".join(f"{y} ${v:,.0f}" for y, v in sorted(years.items())))
    print(f"\nwrote {OUT.name}")


if __name__ == "__main__":
    main()
