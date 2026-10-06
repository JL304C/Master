"""Where the backtest gets its option quotes.

DatabentoSource (real data, run on your laptop)
    OPRA.PILLAR cbbo-1m (consolidated NBBO, 1-minute intervals, history from
    2013-04-01). Downloads only what the backtest needs, at the close:
    - entry days (~1 per week): the whole SPY chain over the last few minutes
      before the close, to pick the expiry and the 10-delta put. Only the slice
      of the chain the strategy can use is kept on disk.
    - every other day: just the open positions' contracts (~10) plus 5 call/put
      pairs within 3% of the money, to get SPY's price from put-call parity.
    Everything is cached as small parquet files in cache/ (tens of MB for the
    whole 2013-present run), so re-runs are free and --offline uses the cache.

LocalChainSource (tests / other vendors)
    A folder of full end-of-day chains, one parquet per trading day with
    columns expiration, right, strike, bid, ask.

cbbo-1m only writes a record when the quote or a trade changes, so each
contract's quote is its last record inside the window before the close.
"""
import os
import re
import threading
import time as _time
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

from pricing import atm_parity_price, fit_forward

HERE = Path(__file__).resolve().parent
ROOT = "SPY"
DATASET, SCHEMA = "OPRA.PILLAR", "cbbo-1m"
CHAIN_WINDOW_MIN = 3      # whole-chain snapshot on entry days
QUOTE_WINDOW_MIN = 15     # per-contract quotes on other days (tiny requests)
KEEP_MAX_DTE = 150        # cached chain slice: expiries out to this many days
DOWNLOAD_TIMEOUT = 300
OCC_RE = re.compile(r"^(\S+)\s*(\d{6})([CP])(\d{8})$")


# --------------------------------------------------------------------------- keys

def load_env():
    """KEY=value lines from .env in this folder, then any sibling folder's .env
    (the other bots and backtests use the same DATABENTO_API_KEY)."""
    found = []
    cands = [HERE / ".env", HERE / ".env.txt"] + sorted(HERE.parent.glob("*/.env")) + sorted(HERE.parent.glob("*/.env.txt"))
    for cand in cands:
        if not cand.is_file() or cand in found:
            continue
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
    return found


# --------------------------------------------------------------------------- helpers

def osi(exp, right, strike):
    """OPRA raw symbol, e.g. 'SPY   261016P00590000'."""
    return f"{ROOT:<6}{exp:%y%m%d}{right}{round(strike * 1000):08d}"


def parse_osi(symbols: pd.Series) -> pd.DataFrame:
    parts = symbols.astype(str).str.extract(OCC_RE)
    return pd.DataFrame({
        "root": parts[0].values,
        "expiration": pd.to_datetime(parts[1], format="%y%m%d", errors="coerce").dt.date.values,
        "right": parts[2].values,
        "strike": (pd.to_numeric(parts[3], errors="coerce") / 1000.0).values,
    }, index=symbols.index)


def last_quotes(df: pd.DataFrame) -> pd.DataFrame:
    """cbbo-1m DataFrame -> one row per symbol: symbol, bid, ask (last record in window)."""
    if df is None or df.empty:
        return pd.DataFrame(columns=["symbol", "bid", "ask"])
    last = df.sort_index().groupby("symbol").tail(1)
    bid = pd.to_numeric(last["bid_px_00"], errors="coerce").astype(float)
    ask = pd.to_numeric(last["ask_px_00"], errors="coerce").astype(float)
    bid = bid.where(bid.between(0, 1e5), 0.0)          # empty side -> no bid
    ask = ask.where(ask.between(0, 1e5) & (ask >= bid))
    return pd.DataFrame({"symbol": last["symbol"].astype(str).values, "bid": bid.values, "ask": ask.values})


def spot_from_chain(chain, d):
    """SPY close ~ K + C - P at the money, on the nearest expiry >= 1 day out."""
    q = chain[(chain["bid"] > 0) & np.isfinite(chain["ask"])]
    for exp in sorted(e for e in q["expiration"].unique() if (e - d).days >= 1):
        K, cm, pm = paired_mids(q, exp)
        if len(K) >= 3:
            return atm_parity_price(K, cm, pm)
    return None


def paired_mids(q, exp):
    """Strikes quoted on both sides at this expiry, with call and put mids."""
    sub = q[q["expiration"] == exp]
    calls = sub[sub["right"] == "C"].drop_duplicates("strike").set_index("strike")
    puts = sub[sub["right"] == "P"].drop_duplicates("strike").set_index("strike")
    common = calls.index.intersection(puts.index)
    cm = (calls.loc[common, "bid"] + calls.loc[common, "ask"]) / 2
    pm = (puts.loc[common, "bid"] + puts.loc[common, "ask"]) / 2
    return common.values, cm.values, pm.values


def forward_for_expiry(q, exp, ref):
    """(F, DF, note) for one expiry: the parity line fit near the money, or if that
    fails, the at-the-money K + C - P with DF = 1."""
    K, cm, pm = paired_mids(q, exp)
    if len(K) < 3:
        return None, None, ""
    F, DF = fit_forward(K, cm, pm, ref_price=ref)
    if F is not None:
        return F, DF, ""
    return atm_parity_price(K, cm, pm), 1.0, "parity line fit failed; used ATM K+C-P forward, DF=1"


def trim_chain(chain, d, spot):
    """Keep only what the strategy can use: puts from 40% below to 3% above spot at
    expiries 45-150 days out (the ~90 DTE pick), plus calls and puts within 5% of
    spot at those expiries and the ones <= 10 days out (parity fits for the
    forward and SPY's price)."""
    dte = chain["expiration"].map(lambda e: (e - d).days)
    k = chain["strike"] / spot
    target = dte.between(45, KEEP_MAX_DTE)
    near = dte.between(0, 10)
    keep = ((chain["right"] == "P") & target & k.between(0.60, 1.03)) | \
           ((target | near) & k.between(0.95, 1.05))
    return chain[keep].reset_index(drop=True)


# --------------------------------------------------------------------------- sources

class LocalChainSource:
    """Full end-of-day chains, one parquet per day (expiration, right, strike, bid, ask)."""

    def __init__(self, data_dir):
        self.dir = Path(data_dir)
        self._day, self._chain = None, None

    def _load(self, d):
        if self._day != d:
            p = self.dir / f"{d}.parquet"
            ch = pd.read_parquet(p) if p.exists() else None
            if ch is not None:
                ch["expiration"] = pd.to_datetime(ch["expiration"]).dt.date
            self._day, self._chain = d, ch
        return self._chain

    def entry_chain(self, d):
        return self._load(d)

    def put_quotes(self, d, contracts):
        ch = self._load(d)
        if ch is None:
            return {}
        p = ch[ch["right"] == "P"]
        idx = {(e, k): (b, a) for e, k, b, a in zip(p["expiration"], p["strike"], p["bid"], p["ask"])}
        return {c: idx[c] for c in contracts if c in idx}

    def spot(self, d):
        ch = self._load(d)
        return spot_from_chain(ch, d) if ch is not None else None

    def first_day(self):
        files = sorted(self.dir.glob("*.parquet"))
        return pd.Timestamp(files[0].stem).date() if files else None

    def last_day(self):
        files = sorted(self.dir.glob("*.parquet"))
        return pd.Timestamp(files[-1].stem).date() if files else None


class DatabentoSource:
    def __init__(self, cache_dir=HERE / "cache", offline=False, log=print):
        self.cache = Path(cache_dir)
        (self.cache / "chains").mkdir(parents=True, exist_ok=True)
        (self.cache / "quotes").mkdir(parents=True, exist_ok=True)
        self.offline = offline
        self.log = log
        self._client = None
        self._closes = None
        self._spot = {}          # date -> spot
        self._expiries = []      # listed expiries from the latest entry chain
        self.requests = 0

    # ---- plumbing
    def client(self):
        if self._client is None:
            import databento as db
            self._client = db.Historical(os.environ["DATABENTO_API_KEY"])
        return self._client

    def set_sessions(self, closes: dict):
        """{date: close timestamp (UTC)} from the exchange calendar (handles early closes)."""
        self._closes = closes

    def window(self, d, minutes):
        end = pd.Timestamp(self._closes[d])
        end = end.tz_localize("UTC") if end.tzinfo is None else end.tz_convert("UTC")
        return end - pd.Timedelta(minutes=minutes), end

    def _get(self, **kw):
        """get_range with a timeout and retries; returns a DataFrame (possibly empty)."""
        box = {}

        def work():
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")      # unresolved symbols, degraded-quality days
                    store = self.client().timeseries.get_range(dataset=DATASET, schema=SCHEMA, **kw)
                    box["df"] = store.to_df()
            except Exception as exc:  # noqa: BLE001
                box["err"] = exc

        for attempt in range(5):
            box.clear()
            th = threading.Thread(target=work, daemon=True)
            th.start()
            th.join(DOWNLOAD_TIMEOUT)
            self.requests += 1
            if "df" in box:
                return box["df"]
            err = box.get("err", f"no response after {DOWNLOAD_TIMEOUT}s")
            msg = str(err).lower()
            if "no data" in msg or "resolve" in msg:
                return pd.DataFrame()
            if attempt == 4:
                raise RuntimeError(f"Databento request failed: {err}")
            _time.sleep(2 ** (attempt + 1))

    # ---- entry-day chain
    def entry_chain(self, d):
        path = self.cache / "chains" / f"{d}.parquet"
        if path.exists():
            chain = pd.read_parquet(path)
            chain["expiration"] = pd.to_datetime(chain["expiration"]).dt.date
        elif self.offline:
            return None
        else:
            start, end = self.window(d, CHAIN_WINDOW_MIN)
            q = last_quotes(self._get(symbols=[f"{ROOT}.OPT"], stype_in="parent", start=start, end=end))
            occ = parse_osi(q["symbol"])
            chain = pd.concat([occ, q[["bid", "ask"]]], axis=1)
            chain = chain[(chain["root"] == ROOT) & chain["expiration"].notna()].drop(columns="root")
            spot = spot_from_chain(chain, d)
            if spot is None:
                # keep it anyway (untrimmed) so a re-run doesn't pay for it again
                self.log(f"{d}: no SPY price from the chain ({len(chain)} contracts); cached untrimmed")
            else:
                chain = trim_chain(chain, d, spot)
            chain.to_parquet(path, index=False)
        self._expiries = sorted(chain["expiration"].unique())
        s = spot_from_chain(chain, d)
        if s:
            self._spot[d] = s
        return chain

    # ---- per-day contract quotes
    def _quotes(self, d, symbols):
        """{symbol: (bid, ask)}; fetches only symbols not cached for that day."""
        path = self.cache / "quotes" / f"{d}.parquet"
        have = pd.read_parquet(path) if path.exists() else pd.DataFrame(columns=["symbol", "bid", "ask"])
        missing = sorted(set(symbols) - set(have["symbol"]))
        if missing and not self.offline:
            start, end = self.window(d, QUOTE_WINDOW_MIN)
            got = last_quotes(self._get(symbols=missing, stype_in="raw_symbol", start=start, end=end))
            # remember symbols with no record in the window, so they aren't re-requested
            none = pd.DataFrame({"symbol": sorted(set(missing) - set(got["symbol"])), "bid": np.nan, "ask": np.nan})
            have = pd.concat([x for x in (have, got, none) if len(x)], ignore_index=True)
            have.to_parquet(path, index=False)
        have = have.dropna(subset=["ask"])
        return {s: (b, a) for s, b, a in zip(have["symbol"], have["bid"], have["ask"]) if s in set(symbols)}

    def put_quotes(self, d, contracts):
        """contracts: [(expiration, strike)] -> {(expiration, strike): (bid, ask)}; also fetches
        the parity pairs used for the day's spot in the same request."""
        syms = {osi(e, "P", k): (e, k) for e, k in contracts}
        pairs = self._parity_symbols(d)
        q = self._quotes(d, list(syms) + pairs) if (syms or pairs) else {}
        self._spot_from_pairs(d, q)
        return {syms[s]: v for s, v in q.items() if s in syms}

    def _parity_symbols(self, d):
        if d in self._spot or not self._spot:
            return []
        ref = self._spot[max(self._spot)]
        # an expiry a few days out, so calls a few % away still have a bid after a big move
        exps = [e for e in self._expiries if (e - d).days >= 3]
        if not exps:
            return []
        e = exps[0]
        out = []
        for k in sorted({round(ref * (1 + x)) for x in (-0.03, -0.015, 0.0, 0.015, 0.03)}):
            out += [osi(e, "C", k), osi(e, "P", k)]
        return out

    def _spot_from_pairs(self, d, q):
        if d in self._spot:
            return
        ests = []
        for s, (b, a) in q.items():
            if s[6 + 6] != "C":
                continue
            p = s[:12] + "P" + s[13:]
            if p in q and b > 0 and q[p][0] > 0:
                k = int(s[13:]) / 1000.0
                ests.append(k + (b + a) / 2 - (q[p][0] + q[p][1]) / 2)
        if ests:
            self._spot[d] = float(np.median(ests))

    def spot(self, d):
        return self._spot.get(d)

    # ---- cost
    def _cost(self, symbols, stype_in, start, end, tries=4):
        """metadata.get_cost with retries (Databento's gateway sometimes answers 504)."""
        for attempt in range(tries):
            try:
                return self.client().metadata.get_cost(dataset=DATASET, schema=SCHEMA, symbols=symbols,
                                                       stype_in=stype_in, start=start, end=end)
            except Exception as exc:  # noqa: BLE001
                if attempt == tries - 1:
                    raise
                self.log(f"  (cost estimate: {str(exc).splitlines()[0][:80]}; retrying)")
                _time.sleep(2 ** (attempt + 1))

    def estimate_cost(self, entry_days, other_days, samples=6):
        """Databento's price for the uncached entry-day chains (sampled), plus a rough
        figure for the small per-day contract requests. Returns (None, reason) if
        Databento can't give an estimate right now."""
        todo = [d for d in entry_days if not (self.cache / "chains" / f"{d}.parquet").exists()]
        todo_q = [d for d in other_days if not (self.cache / "quotes" / f"{d}.parquet").exists()]
        if not todo and not todo_q:
            return 0.0, "everything is cached"
        pick = todo[:: max(1, len(todo) // samples)][:samples] or entry_days[-samples:]
        try:
            costs = []
            for d in pick:
                start, end = self.window(d, CHAIN_WINDOW_MIN)
                costs.append(self._cost([f"{ROOT}.OPT"], "parent", start, end))
            chain_each = float(np.mean(costs))
            # one day's contract request is ~20 symbols x 15 minutes; size it from a real sample
            start, end = self.window(pick[-1], QUOTE_WINDOW_MIN)
            whole = self._cost([f"{ROOT}.OPT"], "parent", start, end)
        except Exception as exc:  # noqa: BLE001
            return None, (f"Databento didn't return a cost estimate ({str(exc).splitlines()[0][:80]}). "
                          f"{len(todo)} entry-day chains and {len(todo_q)} days of contract quotes to download")
        quote_each = whole * 20 / 8000.0       # ~8k+ SPY contracts listed; ~20 requested
        total = chain_each * len(todo) + quote_each * len(todo_q)
        detail = (f"{len(todo)} entry-day chains x ~${chain_each:.4f} + {len(todo_q)} days of contract quotes "
                  f"x ~${quote_each:.5f} (sampled from {len(pick)} days)")
        return total, detail
