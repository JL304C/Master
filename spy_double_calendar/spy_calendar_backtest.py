"""
SPY weekly double calendar backtest on REAL option quotes (Databento OPRA) -- run on your laptop.

Every week (Tuesday, or Wednesday when Tuesday is a holiday) and for every structure in calendar_rules.STRUCTURES (FF, FM, WF):

  entry  10:00 AM ET. Spot = SPY's 9:59 one-minute bar close. Expected move = the mid of the
         at-the-money straddle at the short expiry. Put strike = listed strike nearest
         spot - EM, call strike = nearest spot + EM (x --em-mult). Each strike must be quoted
         at both expiries. The debit is the mid of the four legs (also: natural = pay the asks,
         sell at the bids).
  manage every minute: SPY's one-minute high/low from Alpaca against the strikes, and the
         four legs' consolidated bid/ask (cbbo-1m) for the position's value.
  exit   calendar_rules.simulate: profit targets, strike touch, time stop at 3:45 PM on the
         time-stop day. Fees: $0.03 per contract per leg, open and close.

Every week is traded, whatever VIX or the FOMC calendar say; the filters are then applied
when reporting, so "VIX < 20" and "no filter" are compared on the same trades.

Pricing, reported side by side:
  mid/mid   enter and exit at the mid                      (optimistic)
  mid/nat   enter at the mid, exit at the natural price     (headline, as in bb_real_options.py)
  nat/nat   enter and exit at the natural price             (pessimistic: market orders)

Data: Alpaca SPY daily + one-minute bars (free; cached in cache/), VIX daily closes from
Cboe (FRED as a fallback; cached), FOMC statement days from fomc_dates.txt, Databento
OPRA.PILLAR cbbo-1m quotes for the exact option contracts (cached in cache/opra/).
It prints Databento's cost estimate first and only continues after you type y.

Needs: pip install -r requirements.txt ; ALPACA_API_KEY, ALPACA_SECRET_KEY and
DATABENTO_API_KEY in the .env next to this script.
Run:      python spy_calendar_backtest.py
Options:  --start 2016-01-01  --end 2026-09-30  --structures FF,FM,WF  --em-mult 1.0
          --yes (skip the cost prompt)  --offline (cache only, no downloads)
Output:   spy_calendar_trades.csv + summary tables (also saved to spy_calendar_report.txt).
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import statistics
import sys
import threading
import urllib.request
import warnings
from datetime import date, datetime, time, timedelta
from pathlib import Path

import calendar_rules as R

HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache"
OUT_CSV = HERE / "spy_calendar_trades.csv"
OUT_TXT = HERE / "spy_calendar_report.txt"
FOMC_FILE = HERE / "fomc_dates.txt"

SYMBOL = "SPY"
DATASET, SCHEMA = "OPRA.PILLAR", "cbbo-1m"
ENTRY_TIME = time(10, 0)
EXIT_TIME = time(15, 45)
FEE_PER_LEG = 0.03
FEES = FEE_PER_LEG * 4 * 2             # four legs, opened and closed
MIN_DEBIT = 0.10
DOWNLOAD_TIMEOUT = 180
MODES = ("mid/mid", "mid/nat", "nat/nat")
HEADLINE = "mid/nat"


def arg(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


ENV_ALIASES = {"ALPACA_API_KEY": ("APCA_API_KEY_ID", "ALPACA_KEY", "ALPACA_KEY_ID"),
               "ALPACA_SECRET_KEY": ("APCA_API_SECRET_KEY", "ALPACA_SECRET", "ALPACA_API_SECRET")}
# Folders on the same machine that already hold the same keys (checked after this folder).
SIBLING_ENV_DIRS = ("bollinger_put_spread", "nvda_delayed_condor", "gpc_wheel_bot", "nvda_bull_call_spread_bot")


def load_env():
    """Read KEY=value lines from .env here, then from the sibling bot folders' .env files,
    so keys already set up for the other bots work without copying."""
    found = []
    dirs = [HERE] + [HERE.parent / d for d in SIBLING_ENV_DIRS]
    for folder in dirs:
        for cand in (folder / ".env", folder / ".env.txt", folder / "env", folder / "env.txt"):
            if cand.exists():
                found.append(cand)
                raw = cand.read_bytes()
                text = raw.decode("utf-16") if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else raw.decode("utf-8-sig", "replace")
                for line in text.splitlines():
                    line = line.strip()
                    if line.lower().startswith("export "):
                        line = line[7:].strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    for name, alts in ENV_ALIASES.items():
        for alt in alts:
            if not os.environ.get(name) and os.environ.get(alt):
                os.environ[name] = os.environ[alt]
    return found


def require_keys(names, found):
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        where = "\n  ".join(str(p) for p in found) or "(no .env file found)"
        sys.exit(f"Missing {', '.join(missing)}.\nLooked in:\n  {where}\n"
                 f"Put a .env with these lines in {HERE}:\n" + "".join(f"  {n}=...\n" for n in missing))


def osi(exp: date, right: str, strike: float) -> str:
    """OPRA raw symbol, e.g. 'SPY   261016P00590000'."""
    return f"{SYMBOL:<6}{exp:%y%m%d}{right}{round(strike * 1000):08d}"


# --------------------------------------------------------------------------- #
# data
# --------------------------------------------------------------------------- #
class MarketData:
    """Alpaca bars, Cboe VIX, Databento quotes -- all cached on disk. Times are naive US/Eastern."""

    def __init__(self, offline=False):
        self.offline = offline
        (CACHE / "opra").mkdir(parents=True, exist_ok=True)
        self._db = None
        self._alpaca = None
        self._months = {}

    # ---- stocks (Alpaca) ----
    def _stock_client(self):
        if self._alpaca is None:
            from alpaca.data.historical import StockHistoricalDataClient
            self._alpaca = StockHistoricalDataClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])
        return self._alpaca

    def _bars(self, start: datetime, end: datetime, minute: bool):
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        import pytz
        et = pytz.timezone("America/New_York")
        from datetime import timezone
        # Alpaca's free plan serves SIP data only when it is more than 15 minutes old.
        end_utc = min(et.localize(end).astimezone(timezone.utc), datetime.now(timezone.utc) - timedelta(minutes=16))
        if end_utc <= et.localize(start).astimezone(timezone.utc):
            return []
        req = StockBarsRequest(symbol_or_symbols=SYMBOL, timeframe=TimeFrame.Minute if minute else TimeFrame.Day,
                               start=et.localize(start), end=end_utc,
                               adjustment=Adjustment.RAW, feed=DataFeed.SIP)
        bars = self._stock_client().get_stock_bars(req).data.get(SYMBOL, [])
        return [(b.timestamp.astimezone(et).replace(tzinfo=None), float(b.high), float(b.low), float(b.close))
                for b in bars]

    def trading_days(self, start: date, end: date) -> list[date]:
        path = CACHE / "spy_days.json"
        cached = json.loads(path.read_text()) if path.exists() else None
        if cached and (self.offline or (cached["start"] <= start.isoformat() and cached["fetched"] == date.today().isoformat())):
            return [d for d in map(date.fromisoformat, cached["days"]) if start <= d <= end]
        if self.offline:
            raise RuntimeError(f"{path.name} not cached: run once online first")
        end = min(end, date.today())
        days = sorted({ts.date() for ts, *_ in self._bars(datetime.combine(start, time()),
                                                            datetime.combine(end, time(23, 59)), minute=False)})
        path.write_text(json.dumps(dict(start=start.isoformat(), fetched=date.today().isoformat(),
                                        days=[d.isoformat() for d in days])))
        return days

    def minutes(self, d: date) -> dict:
        """{bar start (ET): (high, low, close)} for regular hours that day."""
        key = (d.year, d.month)
        if key not in self._months:
            path = CACHE / f"spy_1m_{d.year}-{d.month:02d}.json"
            if path.exists():
                rows = json.loads(path.read_text())
            elif self.offline:
                rows = []
            else:
                first = date(d.year, d.month, 1)
                last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
                print(f"  Alpaca SPY 1-minute bars {first:%Y-%m}", flush=True)
                rows = [(ts.isoformat(), h, l, c) for ts, h, l, c in
                        self._bars(datetime.combine(first, time(9, 30)), datetime.combine(last, time(16, 0)), minute=True)
                        if time(9, 30) <= ts.time() < time(16, 0)]
                if last < date.today() - timedelta(days=1):             # only complete months are cached
                    path.write_text(json.dumps(rows))
            by_day = {}
            for ts, h, l, c in rows:
                t = datetime.fromisoformat(ts)
                by_day.setdefault(t.date(), {})[t] = (h, l, c)
            self._months[key] = by_day
        return self._months[key].get(d, {})

    # ---- VIX ----
    INDEX_SOURCES = {
        "VIX": ["https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv",
                "https://fred.stlouisfed.org/graph/fredgraph.csv?id=VIXCLS"],
        "VIX9D": ["https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX9D_History.csv"],
        "VIX3M": ["https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX3M_History.csv",
                  "https://fred.stlouisfed.org/graph/fredgraph.csv?id=VXVCLS"],
    }

    def index(self, name="VIX") -> dict:
        """{date: close} for a Cboe volatility index (VIX, VIX9D, VIX3M); cached for a day.
        VIX itself is required; the others return {} if they can't be had."""
        path = CACHE / ("vix_daily.csv" if name == "VIX" else f"{name.lower()}_daily.csv")
        fresh = path.exists() and datetime.fromtimestamp(path.stat().st_mtime).date() >= date.today() - timedelta(days=1)
        if not fresh and not self.offline:
            for url in self.INDEX_SOURCES[name]:
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                    text = urllib.request.urlopen(req, timeout=60).read().decode("utf-8-sig")
                    rows = _parse_fred(text) if "fred." in url else _parse_cboe(text)
                    if len(rows) > 1000:
                        path.write_text("date,close\n" + "".join(f"{d},{c}\n" for d, c in sorted(rows.items())))
                        break
                except Exception as exc:                                  # noqa: BLE001
                    print(f"  ({name} download from {url.split('/')[2]} failed: {exc})")
        if not path.exists():
            if name == "VIX":
                raise RuntimeError("no VIX history: put a vix_daily.csv (date,close) in cache/")
            print(f"  ({name} history unavailable: its filters will show no trades)")
            return {}
        return {date.fromisoformat(r["date"]): float(r["close"]) for r in csv.DictReader(path.open())}

    def vix(self) -> dict:
        return self.index("VIX")

    # ---- options (Databento) ----
    def _client(self):
        if self._db is None:
            import databento as db
            self._db = db.Historical(os.environ["DATABENTO_API_KEY"])
        return self._db

    @staticmethod
    def _utc(dt: datetime):
        import pandas as pd
        return pd.Timestamp(dt, tz="America/New_York").tz_convert("UTC")

    def _path(self, symbols, start, end):
        key = hashlib.sha1(f"{sorted(symbols)}|{start}|{end}".encode()).hexdigest()[:20]
        return CACHE / "opra" / f"{start:%Y%m%d}_{key}.dbn.zst"

    def is_cached(self, symbols, start, end) -> bool:
        return self._path(symbols, start, end).exists()

    def cost(self, symbols, start, end) -> float:
        return self._client().metadata.get_cost(dataset=DATASET, schema=SCHEMA, symbols=list(symbols),
                                                stype_in="raw_symbol", start=self._utc(start), end=self._utc(end))

    def quotes(self, symbols, start: datetime, end: datetime) -> dict:
        """{raw symbol: [(ts ET, bid, ask), ...]} with only two-sided, uncrossed quotes."""
        import databento as db
        path = self._path(symbols, start, end)
        df = None
        if path.exists() and path.stat().st_size == 0:
            return {}                                             # cached "no data"
        if path.exists():
            try:
                df = db.DBNStore.from_file(path).to_df()
            except Exception:                                             # noqa: BLE001
                path.unlink()
        if df is None:
            if self.offline:
                return {}
            err = None
            for attempt in (1, 2):
                part = path.with_name(path.name + f".part{attempt}")
                part.unlink(missing_ok=True)
                err = self._download(part, symbols, start, end)
                if err is None:
                    part.replace(path)
                    if path.stat().st_size == 0:
                        return {}
                    df = db.DBNStore.from_file(path).to_df()
                    break
                print(f"  (download attempt {attempt} failed: {err})", flush=True)
            if df is None:
                return {}
        out = {}
        if df is None or not len(df):
            return out
        df = df[(df["bid_px_00"] > 0) & (df["ask_px_00"] >= df["bid_px_00"])].sort_index()
        idx = df.index.tz_convert("America/New_York").tz_localize(None)
        for ts, sym, b, a in zip(idx, df["symbol"], df["bid_px_00"], df["ask_px_00"]):
            out.setdefault(str(sym), []).append((ts.to_pydatetime(), float(b), float(a)))
        return out

    def _download(self, part, symbols, start, end):
        box = {}

        def work():
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")               # unresolved symbols, degraded days
                    self._client().timeseries.get_range(dataset=DATASET, schema=SCHEMA, symbols=list(symbols),
                                                        stype_in="raw_symbol", start=self._utc(start),
                                                        end=self._utc(end), path=part)
                box["ok"] = True
            except Exception as exc:                              # noqa: BLE001
                box["err"] = str(exc).splitlines()[0] if str(exc) else type(exc).__name__

        th = threading.Thread(target=work, daemon=True)
        th.start()
        th.join(DOWNLOAD_TIMEOUT)
        if th.is_alive():
            return f"no response after {DOWNLOAD_TIMEOUT}s"
        if "ok" in box and part.exists():
            return None
        if "no data" in box.get("err", "").lower() or "resolve" in box.get("err", "").lower():
            part.write_bytes(b"")                                  # remember "nothing there" -> empty cache file
            return None
        return box.get("err", "download produced no file")


def _parse_cboe(text):
    out = {}
    for r in csv.DictReader(io.StringIO(text)):
        try:
            close = r.get("CLOSE") or r.get(name_col(r))
            out[datetime.strptime(r["DATE"], "%m/%d/%Y").date().isoformat()] = float(close)
        except (KeyError, ValueError, TypeError):
            pass
    return out


def name_col(r):
    """Cboe files without a CLOSE column (e.g. 'DATE,VIX9D') keep the value in the last column."""
    return list(r)[-1]


def _parse_fred(text):
    out = {}
    for r in csv.DictReader(io.StringIO(text)):
        vals = list(r.values())
        try:
            out[date.fromisoformat(vals[0]).isoformat()] = float(vals[1])
        except ValueError:
            pass
    return out


def load_fomc() -> list[date]:
    return [date.fromisoformat(line.split("#")[0].strip()) for line in FOMC_FILE.read_text().splitlines()
            if line.split("#")[0].strip()]


# --------------------------------------------------------------------------- #
# replay
# --------------------------------------------------------------------------- #
def last_quote(series, at: datetime):
    """Last (bid, ask) at or before `at` from a time-sorted [(ts, bid, ask)] list."""
    best = None
    for ts, b, a in series or ():
        if ts > at:
            break
        best = (b, a)
    return best


def mid(q):
    return (q[0] + q[1]) / 2


def prev_close(series: dict, d: date):
    past = [x for x in series if x < d]
    return series[max(past)] if past else None


def vix_percentile(vix: dict, d: date, n=60):
    """Where the previous VIX close sits within the n closes before it (0 = lowest, 1 = highest)."""
    past = sorted(x for x in vix if x < d)[-(n + 1):]
    if len(past) < n // 2:
        return None
    last, hist = vix[past[-1]], [vix[x] for x in past[:-1]]
    return sum(v < last for v in hist) / len(hist)


def vix_context(vix: dict, d: date):
    """Previous close and its 20-day average (what is known at 10 AM on d)."""
    past = sorted(x for x in vix if x < d)[-20:]
    if not past:
        return None, None
    return vix[past[-1]], statistics.fmean(vix[x] for x in past)


class Week:
    """Everything the replay needs for one entry day, built once and shared by the structures."""

    def __init__(self, d, data, vix, vix9d=None, vix3m=None):
        self.d = d
        self.vix9d = prev_close(vix9d, d) if vix9d else None
        self.vix3m = prev_close(vix3m, d) if vix3m else None
        self.vix_pct = vix_percentile(vix, d)
        bars = data.minutes(d)
        bar = bars.get(datetime.combine(d, ENTRY_TIME) - timedelta(minutes=1))
        self.spot = bar[2] if bar else None
        self.vix, self.vix20 = vix_context(vix, d)


def plan(entry: date, s: R.Structure, week: Week, td_set, trading_days, em_mult):
    """Contracts to look at on the entry morning (before any option data is known)."""
    last_known = trading_days[-1]
    # a date after the last known trading day can't be checked for a holiday: take it as listed
    ex = R.expiries(entry, s, lambda d: d in td_set or d > last_known)
    if not ex or week.spot is None or week.vix is None:
        return None
    short, long = ex
    stop = R.time_stop_day(entry, short, s, trading_days)
    if stop is None:
        return None
    dte = (short - entry).days
    em_guess = week.spot * week.vix / 100 * math.sqrt(dte / 365) * em_mult
    half = max(6, math.ceil(em_guess * 0.6))
    atm = round(week.spot)
    kp, kc = round(week.spot - em_guess), round(week.spot + em_guess)
    syms = {osi(short, "C", atm), osi(short, "P", atm)}
    for k in range(kp - half, kp + half + 1):
        syms |= {osi(short, "P", k), osi(long, "P", k)}
    for k in range(kc - half, kc + half + 1):
        syms |= {osi(short, "C", k), osi(long, "C", k)}
    return dict(short=short, long=long, stop=stop, atm=atm, guess=(kp, kc), syms=sorted(syms),
                win=(datetime.combine(entry, ENTRY_TIME) - timedelta(minutes=5), datetime.combine(entry, ENTRY_TIME)))


def build_path(data, entry, stop_day, trading_days, legs, q, vix=None):
    """Minute path of SPY high/low and the position's mid and natural closing values."""
    series = {sym: q.get(sym, []) for sym in legs.values()}
    ptr = {sym: 0 for sym in series}
    state = {}
    path = []
    for d in (x for x in trading_days if entry <= x <= stop_day):
        bars = data.minutes(d)
        for t in sorted(bars):
            if (d == entry and t < datetime.combine(d, ENTRY_TIME)) or (d == stop_day and t >= datetime.combine(d, EXIT_TIME)):
                continue
            end = t + timedelta(minutes=1)
            for sym, ser in series.items():
                i = ptr[sym]
                while i < len(ser) and ser[i][0] <= end:
                    state[sym] = ser[i][1:]
                    i += 1
                ptr[sym] = i
            h, l, _ = bars[t]
            vm = vn = None
            if len(state) == 4:
                ps, pl, cs, cl = (state[legs[k]] for k in ("ps", "pl", "cs", "cl"))
                vm = mid(pl) + mid(cl) - mid(ps) - mid(cs)
                vn = pl[0] + cl[0] - ps[1] - cs[1]            # sell longs at the bid, buy shorts at the ask
            path.append(dict(ts=end, hi=h, lo=l, mid=vm, nat=vn))
        if path and path[-1]["ts"].date() == d:
            path[-1]["eod"] = True
            path[-1]["vix"] = (vix or {}).get(d)                  # that day's VIX close (~15 min after the check)
    return path


def quoted_both(qs, p, right):
    """Strikes with a quote at both the short and the long expiry."""
    ks = {int(sym[-8:]) / 1000 for sym in qs if sym[-9] == right}
    return sorted(k for k in ks if osi(p["short"], right, k) in qs and osi(p["long"], right, k) in qs)


def run_trade(entry, s, p, week, data, fomc, trading_days, em_mult, skips):
    def skip(why):
        skips[(s.name, why)] = skips.get((s.name, why), 0) + 1

    chain = data.quotes(p["syms"], *p["win"])
    at = p["win"][1]
    qs = {sym: last_quote(ser, at) for sym, ser in chain.items()}
    qs = {k: v for k, v in qs.items() if v}
    c_atm, p_atm = qs.get(osi(p["short"], "C", p["atm"])), qs.get(osi(p["short"], "P", p["atm"]))
    if not (c_atm and p_atm):
        skip("no at-the-money quotes (expiry not listed?)")
        return None
    # expected move = the at-the-money straddle's mid (strike within $0.50 of spot)
    em = mid(c_atm) + mid(p_atm)
    put_t, call_t = R.strike_targets(week.spot, em, em_mult)
    put_k = R.pick_strike(put_t, quoted_both(qs, p, "P"), below=week.spot)
    call_k = R.pick_strike(call_t, quoted_both(qs, p, "C"), above=week.spot)
    if put_k is None or call_k is None:
        skip("strike not quoted at both expiries")
        return None
    clipped = abs(put_k - put_t) > 1.5 or abs(call_k - call_t) > 1.5
    legs = dict(ps=osi(p["short"], "P", put_k), pl=osi(p["long"], "P", put_k),
                cs=osi(p["short"], "C", call_k), cl=osi(p["long"], "C", call_k))
    ps, pl, cs, cl = (qs[legs[k]] for k in ("ps", "pl", "cs", "cl"))
    debit_mid = round(mid(pl) + mid(cl) - mid(ps) - mid(cs), 4)
    debit_nat = round(pl[1] + cl[1] - ps[0] - cs[0], 4)
    if debit_mid < MIN_DEBIT:
        skip(f"debit < ${MIN_DEBIT:.2f}")
        return None
    hold = data.quotes(list(legs.values()), p["win"][1], datetime.combine(p["stop"], time(16, 0)))
    path = build_path(data, entry, p["stop"], trading_days, legs, hold, week.vix_series)
    if not path:
        skip("no SPY minute bars during the trade")
        return None
    t = dict(structure=s.name, entry=entry, short_exp=p["short"], long_exp=p["long"], time_stop=p["stop"],
             spot=round(week.spot, 2), vix=week.vix, vix20=round(week.vix20, 2), em=round(em, 2),
             em_pct=round(em / week.spot, 4), put_k=put_k, call_k=call_k, strike_clipped=clipped,
             debit_mid=round(debit_mid, 2), debit_nat=round(debit_nat, 2),
             fomc=R.fomc_in_window(entry, p["stop"], fomc),
             vix9d=week.vix9d, vix3m=week.vix3m, vix_pct60=week.vix_pct)
    for mode in MODES:
        debit = debit_mid if mode.startswith("mid") else debit_nat
        key = "mid" if mode.endswith("mid") else "nat"
        fills = R.simulate(debit, [dict(ts=m["ts"], hi=m["hi"], lo=m["lo"], val=m[key]) for m in path],
                           put_k, call_k, R.SPEC_EXITS)
        if fills is None:
            skip("no option quotes during the trade")
            return None
        t[f"pnl {mode}"] = R.pnl(fills, debit, FEES)
        t[f"ret {mode}"] = round(t[f"pnl {mode}"] / (debit * 100), 4)
        t[f"exit {mode}"] = R.final_reason(fills)
        t[f"exit_ts {mode}"] = R.exit_ts(fills)
    path_h = [dict(ts=m["ts"], hi=m["hi"], lo=m["lo"], val=m["nat"], vix=m.get("vix")) for m in path]
    t["grid"] = {r.name: R.pnl(R.simulate(debit_mid, path_h, put_k, call_k, r, week.vix), debit_mid, FEES)
                 for r in R.EXIT_GRID}
    # his profit curve: the position's mid value vs the mid debit at each day's last check, and
    # whether SPY had touched a strike by then
    curve, touched = [], False
    for m in path:
        touched = touched or (m["hi"] is not None and (m["hi"] >= call_k or m["lo"] <= put_k))
        if m.get("eod") and m["mid"] is not None:
            curve.append((round(m["mid"] / debit_mid - 1, 4), touched))
    t["curve"] = curve
    return t


def replay(data, start, end, structures, em_mult, assume_yes=False, log=print):
    trading_days = data.trading_days(start - timedelta(days=10), end + timedelta(days=30))
    td_set = set(trading_days)
    vix = data.vix()
    vix9d, vix3m = data.index("VIX9D"), data.index("VIX3M")
    fomc = load_fomc()
    weeks, plans = {}, []
    for d in R.entry_days(trading_days):
        if not (start <= d <= end):
            continue
        weeks[d] = Week(d, data, vix, vix9d, vix3m)
        weeks[d].vix_series = vix
        for name in structures:
            p = plan(d, R.STRUCTURES[name], weeks[d], td_set, trading_days, em_mult)
            if p and p["stop"] <= trading_days[-1] and p["stop"] < date.today():
                plans.append((d, R.STRUCTURES[name], p))
    log(f"{len(weeks)} entry days, {len(plans)} trades to replay ({', '.join(structures)})")

    if not data.offline and not confirm_cost(data, plans, assume_yes, log):
        return [], {}

    trades, skips = [], {}
    for n, (d, s, p) in enumerate(plans, 1):
        t = run_trade(d, s, p, weeks[d], data, fomc, trading_days, em_mult, skips)
        if t:
            trades.append(t)
            log(f"  [{n}/{len(plans)}] {s.name} {d} {t['put_k']:g}P/{t['call_k']:g}C {p['short']:%m-%d}/{p['long']:%m-%d} "
                f"VIX {t['vix']:5.2f} debit {t['debit_mid']:.2f} -> {t['exit ' + HEADLINE]:<26} ${t['pnl ' + HEADLINE]:>7,.0f}")
    return trades, skips


def confirm_cost(data, plans, assume_yes, log):
    todo = [(d, s, p) for d, s, p in plans if not data.is_cached(p["syms"], *p["win"])]
    if not todo:
        return True
    sample = todo[:: max(1, len(todo) // 4)][:4]
    try:
        c_entry = statistics.fmean(data.cost(p["syms"], *p["win"]) for _, _, p in sample)
        c_hold = statistics.fmean(
            data.cost([osi(p["short"], "P", p["guess"][0]), osi(p["long"], "P", p["guess"][0]),
                       osi(p["short"], "C", p["guess"][1]), osi(p["long"], "C", p["guess"][1])],
                      p["win"][1], datetime.combine(p["stop"], time(16, 0))) for _, _, p in sample)
    except Exception as exc:                                             # noqa: BLE001
        log(f"(no Databento cost estimate: {str(exc).splitlines()[0]})")
        c_entry = c_hold = float("nan")
    est = len(todo) * (c_entry + c_hold)
    log(f"Databento estimate for {len(todo)} uncached trades: ~${est:,.2f} "
        f"(~${c_entry:.4f} entry chain + ~${c_hold:.4f} holding window each, from {len(sample)} samples)")
    if assume_yes:
        return True
    return input("Continue? [y/N] ").strip().lower() == "y"


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #
def stats(pnls, rets=None):
    if not pnls:
        return None
    eq = peak = dd = 0.0
    for x in pnls:
        eq += x
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    sd = statistics.stdev(pnls) if len(pnls) > 1 else 0.0
    return dict(n=len(pnls), win=sum(x > 0 for x in pnls) / len(pnls), total=sum(pnls), avg=statistics.fmean(pnls),
                ret=statistics.fmean(rets) if rets else float("nan"), worst=min(pnls), dd=dd,
                t=statistics.fmean(pnls) / (sd / math.sqrt(len(pnls))) if sd else float("nan"))


def row(label, s, extra=""):
    if not s:
        return f"  {label:<50} {'(no trades)':>8}"
    return (f"  {label:<50} {s['n']:>4} {s['win']:>5.0%} {s['ret']:>+7.1%} {s['avg']:>+7.1f} {s['total']:>+9,.0f} "
            f"{s['worst']:>+8,.0f} {s['dd']:>+9,.0f} {s['t']:>+5.1f}{extra}")


HEAD = (f"  {'':<50} {'n':>4} {'win':>5} {'avg ret':>7} {'avg $':>7} {'total $':>9} {'worst':>8} {'max DD':>9} {'t':>5}"
        f"   mid/mid total | nat/nat total")

FILTERS = [
    ("all weeks (no filter)", lambda t: True),
    ("VIX < 20", lambda t: t["vix"] < R.VIX_MAX),
    ("VIX < 20, no FOMC  <- the spec", lambda t: t["vix"] < R.VIX_MAX and not t["fomc"]),
    ("VIX < 20 and <= its 20-day avg, no FOMC", lambda t: t["vix"] < R.VIX_MAX and t["vix"] <= t["vix20"] and not t["fomc"]),
    ("no FOMC (any VIX)", lambda t: not t["fomc"]),
    ("VIX >= 20, no FOMC", lambda t: t["vix"] >= R.VIX_MAX and not t["fomc"]),
    ("VIX in lower half of its 60-day range, no FOMC",
     lambda t: t["vix_pct60"] is not None and t["vix_pct60"] <= 0.5 and not t["fomc"]),
    ("VIX in upper half of its 60-day range, no FOMC",
     lambda t: t["vix_pct60"] is not None and t["vix_pct60"] > 0.5 and not t["fomc"]),
    ("VIX9D > VIX (front stress), no FOMC", lambda t: t["vix9d"] and t["vix9d"] > t["vix"] and not t["fomc"]),
    ("VIX9D <= VIX (normal), no FOMC", lambda t: t["vix9d"] and t["vix9d"] <= t["vix"] and not t["fomc"]),
    ("VIX >= 0.95 x VIX3M (flat/inverted), no FOMC",
     lambda t: t["vix3m"] and t["vix"] >= 0.95 * t["vix3m"] and not t["fomc"]),
    ("VIX < 0.95 x VIX3M (contango), no FOMC", lambda t: t["vix3m"] and t["vix"] < 0.95 * t["vix3m"] and not t["fomc"]),
]


def report(trades, skips, structures, start, end, log):
    trades = sorted(trades, key=lambda t: t["entry"])
    log("")
    log(f"SPY weekly double calendar, real OPRA quotes, {start} .. {end}, 1 double calendar per trade, fees ${FEES:.2f}")
    log(f"Headline pricing = {HEADLINE} (enter at the mid, exit at the natural price). 'avg ret' = P&L / debit.")
    for name in structures:
        ts = [t for t in trades if t["structure"] == name]
        log("")
        log(f"{name}: {R.STRUCTURES[name].label}   ({len(ts)} trades replayed)")
        log(HEAD)
        for label, f in FILTERS:
            sub = [t for t in ts if f(t)]
            extra = ""
            if sub:
                extra = (f"   {sum(t['pnl mid/mid'] for t in sub):>+13,.0f} | {sum(t['pnl nat/nat'] for t in sub):>+13,.0f}")
            log(row(label, stats([t["pnl " + HEADLINE] for t in sub], [t["ret " + HEADLINE] for t in sub]), extra))

    by = {(t["structure"], t["entry"]): t for t in trades}
    if {"FF", "WF"} <= set(structures):
        log("")
        log("His full plan: VIX < 20 -> FF, VIX >= 20 -> WF (the Wednesday trick); no FOMC weeks")
        log(HEAD)
        combo = []
        for d in sorted({t["entry"] for t in trades}):
            ff, wf = by.get(("FF", d)), by.get(("WF", d))
            pick = ff if ff and ff["vix"] < R.VIX_MAX else wf if wf and wf["vix"] >= R.VIX_MAX else None
            if pick and not pick["fomc"]:
                combo.append(pick)
        log(row("FF/WF switch on VIX", stats([t["pnl " + HEADLINE] for t in combo], [t["ret " + HEADLINE] for t in combo])))
        both = [t for d in sorted({t["entry"] for t in trades}) for t in (by.get(("FF", d)), by.get(("WF", d)))
                if t and t["vix"] >= R.VIX_MAX and not t["fomc"] and by.get(("FF", d)) and by.get(("WF", d))]
        log(row("VIX >= 20: run both FF and WF", stats([t["pnl " + HEADLINE] for t in both], [t["ret " + HEADLINE] for t in both])))

    spec = [t for t in trades if t["structure"] == structures[0] and t["vix"] < R.VIX_MAX and not t["fomc"]]
    if spec:
        log("")
        log(f"Exit rules on the spec trades ({structures[0]}, VIX < 20, no FOMC), mid/nat:")
        log(HEAD)
        for r in R.EXIT_GRID:
            pn = [t["grid"][r.name] for t in spec]
            log(row(r.name, stats(pn, [p / (t["debit_mid"] * 100) for p, t in zip(pn, spec)])))
        log("")
        log("His profit curve (~+10% by day 3, ~+30% after a week if SPY stays between the strikes) vs the real")
        log(f"mid value of the {structures[0]} trades, by trading day after entry (all weeks, no exits applied):")
        log(f"  {'day':>5} {'never touched: n':>17} {'avg':>7} {'median':>7} {'> 0':>5}   {'touched: n':>11} {'avg':>7}")
        allt = [t for t in trades if t["structure"] == structures[0]]
        for k in range(6):
            nt = [t["curve"][k][0] for t in allt if len(t["curve"]) > k and not t["curve"][k][1]]
            tt = [t["curve"][k][0] for t in allt if len(t["curve"]) > k and t["curve"][k][1]]
            if not nt and not tt:
                break
            a = (f"{len(nt):>17} {statistics.fmean(nt):>+7.1%} {statistics.median(nt):>+7.1%} "
                 f"{sum(x > 0 for x in nt) / len(nt):>5.0%}") if nt else f"{0:>17} {'':>21}"
            b = f"{len(tt):>11} {statistics.fmean(tt):>+7.1%}" if tt else f"{0:>11}"
            log(f"  {k + 1:>5} {a}   {b}")
        log("")
        log("Exit reasons (spec trades, mid/nat):")
        reasons = {}
        for t in spec:
            k = t["exit " + HEADLINE]
            reasons.setdefault(k, []).append(t["pnl " + HEADLINE])
        for k, v in sorted(reasons.items(), key=lambda kv: -len(kv[1])):
            log(f"  {k:<40} {len(v):>4}  avg ${statistics.fmean(v):>+8,.1f}")
        log("")
        log("By year (spec trades, mid/nat):")
        log(HEAD)
        for y in sorted({t["entry"].year for t in spec}):
            sub = [t for t in spec if t["entry"].year == y]
            log(row(str(y), stats([t["pnl " + HEADLINE] for t in sub], [t["ret " + HEADLINE] for t in sub])))
        clipped = sum(t["strike_clipped"] for t in spec)
        log("")
        log(f"Typical spec trade: debit ${statistics.median(t['debit_mid'] for t in spec):.2f} mid "
            f"(${statistics.median(t['debit_nat'] for t in spec):.2f} natural), strikes "
            f"±{statistics.median(t['em_pct'] for t in spec):.1%} from spot; {clipped} trades had a strike >$1.50 off target.")
    if skips:
        log("")
        log("Weeks skipped:")
        for (name, why), n in sorted(skips.items()):
            log(f"  {name} {why}: {n}")


def write_csv(trades):
    cols = [k for k in trades[0] if k not in ("grid", "curve")] + [f"grid: {r.name}" for r in R.EXIT_GRID]
    with OUT_CSV.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for t in sorted(trades, key=lambda t: (t["entry"], t["structure"])):
            w.writerow([t[c] if c in t else t["grid"][c[6:]] for c in cols])


def main():
    found = load_env()
    if "--offline" not in sys.argv:
        require_keys(["ALPACA_API_KEY", "ALPACA_SECRET_KEY", "DATABENTO_API_KEY"], found)
    start = date.fromisoformat(arg("--start", "2016-01-01"))
    end = date.fromisoformat(arg("--end", (date.today() - timedelta(days=14)).isoformat()))
    structures = arg("--structures", "FF,FM,WF,FF2").upper().split(",")
    em_mult = float(arg("--em-mult", "1.0"))
    data = MarketData(offline="--offline" in sys.argv)
    lines = []

    def log(s=""):
        print(s, flush=True)
        lines.append(s)

    trades, skips = replay(data, start, end, structures, em_mult, "--yes" in sys.argv, log)
    if not trades:
        log("No trades replayed.")
        return
    report(trades, skips, structures, start, end, log)
    write_csv(trades)
    OUT_TXT.write_text("\n".join(lines) + "\n")
    print(f"\nWrote {OUT_CSV.name} and {OUT_TXT.name}")


if __name__ == "__main__":
    main()
