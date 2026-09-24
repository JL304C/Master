"""
NQ/MNQ liquidity-sweep scalper -- rules-based backtest engine.

Implements the strategy spec as an explicit per-bar state machine on 1-minute
OHLC bars (all times US/Eastern):

  WAIT_FOR_SESSION -> IDENTIFY_LIQUIDITY -> WAIT_FOR_SWEEP -> WAIT_FOR_RECLAIM
  -> WAIT_FOR_FVG_CONFIRMATION -> PLACE_ENTRY -> MANAGE_POSITION -> (LOCKOUT)

Long (shorts are the mirror image):
  1. Liquidity: untaken sell-side levels known at window start -- Asian low
     (20:00-24:00), prior-day low, London low (NY window only), confirmed
     overnight swing lows, equal-low clusters -- plus swing lows confirmed
     during the window.
  2. Sweep: a bar trades >= 1 tick through the level and a bar closes back
     above it within RECLAIM_BARS (inside a London 02:00-05:00 or NY
     09:30-11:00 window).
  3. Displacement + FVG inversion: within CONFIRM_BARS, a bullish candle whose
     body >= DISP_MULT x median body of the prior 20 bars closes >= 1 tick
     above the top of a bearish 3-candle FVG formed in the down-leg.
  4. Entry: aggressive = market at next bar open; conservative = limit at the
     top of the inverted FVG (the retest), valid RETEST_BARS.
  5. Stop: 1 tick beyond the sweep extreme.  Reject if stop distance >
     session cap or < MIN_STOP.  Contracts = floor(risk$ / (stop_pts * $/pt)).
  6. Target 1: nearest opposing untaken liquidity (Asian/London/prior-day
     highs, swing highs, equal highs, midnight open, RTH-gap edge, 1H FVG
     edge).  Must be >= MIN_RR x risk (literal spec) or else the trade is
     skipped.  Half off at T1, stop to break-even, rest at T2 (next pool)
     or time stop.
  7. Risk: max attempts per window, daily $ loss cutoff -> LOCKOUT.

Fill assumptions (deliberately pessimistic):
  - market entries / stop exits / time exits pay SLIPPAGE_TICKS each.
  - limit orders (conservative entry, targets) fill only if price trades
    THROUGH the limit by 1 tick (queue-position haircut), at the limit.
  - if one bar touches both stop and target, the stop is assumed hit first.
  - on the bar a limit entry fills, a stop touch counts; a target touch
    does not.
No information from bar i+1 is used to decide anything at bar i
(verified by the truncation test in scalper_audit.py).

Usage:
  python scalper_backtest.py --csv NQ_1min.csv [--tz UTC] [--instrument MNQ]
CSV: header row containing a timestamp column (ts_event / timestamp /
datetime / time / date) and open, high, low, close.  Databento ohlcv-1m
exports work as-is (ts_event in UTC -> pass --tz UTC).  Bar timestamps are
treated as bar OPEN times.
"""
import csv, math, statistics, sys, json, argparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, time as dtime, timezone

# ---------------------------------------------------------------- config
INSTRUMENTS = {
    # tick, $ per point, round-turn commission+exchange fees per contract
    'MNQ': dict(tick=0.25, point_value=2.0, rt_fee=1.24),
    'NQ':  dict(tick=0.25, point_value=20.0, rt_fee=4.50),
}

@dataclass
class Config:
    instrument: str = 'MNQ'
    account: float = 26000.0
    risk_per_trade: float = 260.0        # 1% of account at the stop
    daily_loss_limit: float = 520.0      # 2R -> lockout for the rest of the day
    max_attempts_per_window: int = 2
    max_contracts: int = 50

    # windows (ET): (name, start, end, max stop points)
    london: tuple = ('LONDON', dtime(2, 0), dtime(5, 0), 20.0)
    ny: tuple = ('NY', dtime(9, 30), dtime(11, 0), 30.0)
    use_london: bool = True
    use_ny: bool = True
    min_stop_pts: float = 3.0

    swing_strength: int = 5              # pivot = extreme of +/-5 bars
    eq_tol_ticks: int = 3                # equal highs/lows within 3 ticks
    sweep_ticks: int = 1                 # must trade >= 1 tick through level
    reclaim_bars: int = 3                # close back inside within 3 bars
    confirm_bars: int = 15               # displacement/inversion deadline
    fvg_lookback: int = 20               # bearish FVG must form within 20 bars before sweep
    disp_mult: float = 1.75              # body >= 1.75 x median(prev 20 bodies)
    htf_filter: bool = True              # longs only below prior-day midpoint, shorts above
    entry_mode: str = 'conservative'     # 'conservative' (retest limit) | 'aggressive'
    retest_bars: int = 10
    min_rr: float = 2.0
    target_mode: str = 'nearest'         # 'nearest' (literal: nearest pool must be >=2R)
                                         # | 'first_beyond' (first pool that is >=2R)
    partial_frac: float = 0.5
    max_hold_bars: int = 60              # time stop

    slippage_ticks: int = 1
    apply_costs: bool = True

    @property
    def spec(self):
        return INSTRUMENTS[self.instrument]


# ---------------------------------------------------------------- data
class Bars:
    """Column-oriented 1-minute bars with ET timestamps (naive, ET wall clock)."""
    def __init__(self, ts, o, h, l, c):
        self.ts, self.o, self.h, self.l, self.c = ts, o, h, l, c
        self.n = len(ts)
        # futures session date: 18:00 ET starts the next day's session
        self.sdate = [(t + timedelta(hours=6)).date() for t in ts]
        self.tod = [t.time() for t in ts]

    def slice(self, end):
        return Bars(self.ts[:end], self.o[:end], self.h[:end], self.l[:end], self.c[:end])


def load_csv(path, tz='America/New_York'):
    """Loads OHLC 1-min CSV. tz = timezone of the timestamps in the file."""
    conv = None
    if tz not in ('America/New_York', 'ET', 'EST', 'US/Eastern'):
        from zoneinfo import ZoneInfo        # Windows: pip install tzdata
        src, et = ZoneInfo(tz) if tz != 'UTC' else timezone.utc, ZoneInfo('America/New_York')
        conv = lambda d: (d.replace(tzinfo=src) if d.tzinfo is None else d).astimezone(et).replace(tzinfo=None)
    rows = []
    with open(path, newline='') as f:
        rd = csv.DictReader(f)
        cols = {k.lower().strip(): k for k in rd.fieldnames}
        tcol = next(cols[k] for k in ('ts_event', 'timestamp', 'datetime', 'time', 'date') if k in cols)
        sym_col = cols.get('symbol')
        for r in rd:
            if sym_col and ('-' in r[sym_col]):  # skip calendar spreads in Databento exports
                continue
            raw = r[tcol].strip()
            if raw.isdigit():                    # unix ns / s
                v = int(raw)
                d = datetime.fromtimestamp(v / 1e9 if v > 1e12 else v, timezone.utc)
                if conv is None:
                    raise SystemExit('numeric timestamps are UTC -- pass --tz UTC')
            else:
                raw = raw.replace('Z', '+00:00')
                if '.' in raw and '+' in raw:    # trim ns precision
                    head, tail = raw.split('+', 1)
                    raw = head.split('.')[0] + '+' + tail
                d = datetime.fromisoformat(raw)
            d = conv(d) if conv else d.replace(tzinfo=None)
            rows.append((d, float(r[cols['open']]), float(r[cols['high']]),
                         float(r[cols['low']]), float(r[cols['close']])))
    rows.sort(key=lambda x: x[0])
    # de-dup (keep first per minute -- front-month in a continuous export)
    out, last = [], None
    for row in rows:
        if row[0] != last:
            out.append(row); last = row[0]
    return Bars(*[list(x) for x in zip(*out)])


# ---------------------------------------------------------------- helpers
def in_range(t, a, b):
    """a <= t < b, handling ranges that cross midnight."""
    return a <= t < b if a < b else (t >= a or t < b)


def pivots(bars, k):
    """Pivot highs/lows. Returns lists of (confirm_idx, pivot_idx, price).
    A pivot at i is only KNOWN at i+k (no lookahead)."""
    ph, pl = [], []
    h, l = bars.h, bars.l
    for i in range(k, bars.n - k):
        hi = h[i]
        if all(h[j] < hi for j in range(i - k, i)) and all(h[j] <= hi for j in range(i + 1, i + k + 1)):
            ph.append((i + k, i, hi))
        lo = l[i]
        if all(l[j] > lo for j in range(i - k, i)) and all(l[j] >= lo for j in range(i + 1, i + k + 1)):
            pl.append((i + k, i, lo))
    return ph, pl


def hourly_fvgs(bars, upto, start):
    """1H FVGs from bars[start:upto] aggregated to clock hours.
    Returns (bullish_gaps, bearish_gaps) as (lo, hi) tuples, only fully
    completed hours."""
    hours, cur, key = [], None, None
    for i in range(start, upto):
        t = bars.ts[i]
        k = (t.date(), t.hour)
        if k != key:
            if cur: hours.append(cur)
            cur, key = [bars.h[i], bars.l[i]], k
        else:
            cur[0] = max(cur[0], bars.h[i]); cur[1] = min(cur[1], bars.l[i])
    # the last (possibly incomplete) hour is dropped
    bull, bear = [], []
    for j in range(2, len(hours)):
        a, c = hours[j - 2], hours[j]
        if c[1] > a[0]: bull.append((a[0], c[1]))
        if c[0] < a[1]: bear.append((c[0], a[1]))
    return bull, bear


# ---------------------------------------------------------------- engine
@dataclass
class Trade:
    side: int; window: str; day: str
    sweep_level: float; level_kind: str
    signal_idx: int; entry_idx: int; entry: float; stop: float
    t1: float; t2: float; contracts: int
    init_stop: float = 0.0
    exits: list = field(default_factory=list)   # (idx, price, qty, reason)
    pnl: float = 0.0; gross: float = 0.0; r_mult: float = 0.0


def run(bars, cfg=Config(), day_filter=None):
    """Runs the strategy over bars.  Returns list[Trade] and per-day P&L dict.
    day_filter: optional set of session dates to trade (for walk-forward)."""
    spec = cfg.spec
    tick, pv = spec['tick'], spec['point_value']
    slip = cfg.slippage_ticks * tick if cfg.apply_costs else 0.0
    fee_side = spec['rt_fee'] / 2 if cfg.apply_costs else 0.0
    n = bars.n
    H, L, O, C = bars.h, bars.l, bars.o, bars.c
    ph_all, pl_all = pivots(bars, cfg.swing_strength)

    # index sessions
    sess = {}                               # sdate -> (first_idx, last_idx)
    for i, d in enumerate(bars.sdate):
        if d not in sess: sess[d] = [i, i]
        else: sess[d][1] = i
    days = sorted(sess)

    trades, daily = [], {}
    for di, d in enumerate(days):
        if day_filter is not None and d not in day_filter:
            continue
        s0, s1 = sess[d]
        prev = sess.get(days[di - 1]) if di > 0 else None
        if prev is None:
            continue
        daily.setdefault(d, 0.0)
        pdh = max(H[prev[0]:prev[1] + 1]); pdl = min(L[prev[0]:prev[1] + 1])
        pd_mid = (pdh + pdl) / 2
        # prior RTH close = close of last bar before 16:00 in prev session
        rth_close = None
        for i in range(prev[1], prev[0] - 1, -1):
            if bars.tod[i] < dtime(16, 0) and bars.tod[i] >= dtime(9, 30):
                rth_close = C[i]; break

        asian = [i for i in range(s0, s1 + 1) if in_range(bars.tod[i], dtime(20, 0), dtime(0, 0))]
        midnight_idx = next((i for i in range(s0, s1 + 1) if bars.tod[i] >= dtime(0, 0) and bars.ts[i].date() == d), None)
        london_idx = [i for i in range(s0, s1 + 1) if in_range(bars.tod[i], dtime(2, 0), dtime(5, 0))]

        windows = []
        if cfg.use_london: windows.append(cfg.london)
        if cfg.use_ny: windows.append(cfg.ny)
        locked = False
        for (wname, wstart, wend, max_stop) in windows:
            if locked: break
            widx = [i for i in range(s0, s1 + 1) if in_range(bars.tod[i], wstart, wend)
                    and bars.ts[i].date() == d]
            if not widx: continue
            w0, w1 = widx[0], widx[-1]

            # ---------- IDENTIFY_LIQUIDITY (only info available before w0)
            def untaken(price, formed, side):
                # side +1 = buy-side (above), -1 = sell-side (below)
                if formed + 1 >= w0: return True
                if side > 0: return max(H[formed + 1:w0]) < price
                return min(L[formed + 1:w0]) > price
            buy, sell = [], []        # (price, kind, formed_idx)
            if asian:
                ai = [i for i in asian if i < w0]
                if ai:
                    hi_i = max(ai, key=lambda i: H[i]); lo_i = min(ai, key=lambda i: L[i])
                    buy.append((H[hi_i], 'ASIA_H', ai[-1])); sell.append((L[lo_i], 'ASIA_L', ai[-1]))
            buy.append((pdh, 'PDH', s0 - 1)); sell.append((pdl, 'PDL', s0 - 1))
            if wname == 'NY':
                li = [i for i in london_idx if i < w0]
                if li:
                    buy.append((max(H[i] for i in li), 'LON_H', li[-1]))
                    sell.append((min(L[i] for i in li), 'LON_L', li[-1]))
            sw_h = [(p, 'SWING_H', pi) for (ci, pi, p) in ph_all if s0 <= pi and ci < w0]
            sw_l = [(p, 'SWING_L', pi) for (ci, pi, p) in pl_all if s0 <= pi and ci < w0]
            # equal highs/lows: clusters of >=2 swing pivots within tol
            tol = cfg.eq_tol_ticks * tick
            for src, dst, kind, pick in ((sw_h, buy, 'EQH', max), (sw_l, sell, 'EQL', min)):
                ps = sorted(src)
                for a in range(len(ps)):
                    for b in range(a + 1, len(ps)):
                        if abs(ps[b][0] - ps[a][0]) <= tol:
                            dst.append((pick(ps[a][0], ps[b][0]), kind, max(ps[a][2], ps[b][2])))
            buy += sw_h; sell += sw_l
            buy = [x for x in buy if untaken(x[0], x[2], +1)]
            sell = [x for x in sell if untaken(x[0], x[2], -1)]
            # target-only reference levels
            ref = []
            if midnight_idx is not None and midnight_idx < w0: ref.append(O[midnight_idx])
            if wname == 'NY' and rth_close is not None: ref.append(rth_close)
            bull1h, bear1h = hourly_fvgs(bars, w0, max(prev[0], s0 - 1440))
            for lo_, hi_ in bull1h + bear1h: ref += [lo_, hi_]

            # dynamic in-window pivots (known only after confirmation)
            win_ph = [(ci, pi, p) for (ci, pi, p) in ph_all if w0 <= ci <= w1 + cfg.max_hold_bars]
            win_pl = [(ci, pi, p) for (ci, pi, p) in pl_all if w0 <= ci <= w1 + cfg.max_hold_bars]

            consumed = set()
            attempts = 0
            state = 'WAIT_FOR_SWEEP'
            setup = None
            pos = None
            order = None
            i = w0
            end_idx = min(w1 + cfg.max_hold_bars, s1)
            while i <= end_idx:
                in_win = i <= w1
                # current levels = static + pivots confirmed by bar i-1
                if state == 'WAIT_FOR_SWEEP' and in_win and not locked and attempts < cfg.max_attempts_per_window:
                    lv_buy = buy + [(p, 'SWING_H', pi) for (ci, pi, p) in win_ph if ci < i]
                    lv_sell = sell + [(p, 'SWING_L', pi) for (ci, pi, p) in win_pl if ci < i]
                    # SELL-SIDE sweep -> long setup
                    cand = []
                    for (p, kind, fi) in lv_sell:
                        key = ('S', round(p / tick))
                        if key in consumed or fi >= i: continue
                        if L[i] <= p - cfg.sweep_ticks * tick and (i == w0 or min(L[max(fi + 1, w0):i] or [p + 1]) > p):
                            cand.append((+1, p, kind, key))
                    for (p, kind, fi) in lv_buy:
                        key = ('B', round(p / tick))
                        if key in consumed or fi >= i: continue
                        if H[i] >= p + cfg.sweep_ticks * tick and (i == w0 or max(H[max(fi + 1, w0):i] or [p - 1]) < p):
                            cand.append((-1, p, kind, key))
                    if cand:
                        # most significant: the level furthest through (largest pool swept this bar)
                        # deepest pool swept this bar: lowest sell-side / highest buy-side level
                        side, p, kind, key = max(cand, key=lambda c: -c[1] if c[0] > 0 else c[1])
                        for c in cand: consumed.add(c[3])
                        if cfg.htf_filter and ((side > 0 and p > pd_mid) or (side < 0 and p < pd_mid)):
                            pass
                        else:
                            setup = dict(side=side, level=p, kind=kind, pierce=i,
                                         ext=L[i] if side > 0 else H[i], reclaimed=None)
                            state = 'WAIT_FOR_RECLAIM'
                if state == 'WAIT_FOR_RECLAIM':
                    s = setup
                    s['ext'] = min(s['ext'], L[i]) if s['side'] > 0 else max(s['ext'], H[i])
                    if (s['side'] > 0 and C[i] > s['level']) or (s['side'] < 0 and C[i] < s['level']):
                        s['reclaimed'] = i; state = 'WAIT_FOR_FVG_CONFIRMATION'
                    elif i - s['pierce'] >= cfg.reclaim_bars - 1:
                        state, setup = 'WAIT_FOR_SWEEP', None
                if state == 'WAIT_FOR_FVG_CONFIRMATION' and i >= setup['reclaimed']:
                    s = setup; side = s['side']
                    s['ext'] = min(s['ext'], L[i]) if side > 0 else max(s['ext'], H[i])
                    # acceptance back beyond the level on a later bar -> sweep failed
                    if i > s['reclaimed'] and ((side > 0 and C[i] < s['level']) or (side < 0 and C[i] > s['level'])):
                        state, setup = 'WAIT_FOR_SWEEP', None
                    elif i - s['reclaimed'] > cfg.confirm_bars or not in_win:
                        state, setup = 'WAIT_FOR_SWEEP', None
                    else:
                        body = abs(C[i] - O[i])
                        prior = [abs(C[j] - O[j]) for j in range(max(0, i - 20), i)]
                        med = statistics.median(prior) if prior else 0
                        disp = (C[i] - O[i]) * side > 0 and body >= cfg.disp_mult * max(med, tick)
                        fvg = None
                        if disp:
                            # opposing FVGs formed in the leg into the sweep (candle-3 index k < i)
                            # that were still intact -- no close back through them -- until bar i
                            best = None
                            for k in range(max(2, s['pierce'] - cfg.fvg_lookback), i):
                                if side > 0 and H[k] < L[k - 2]:
                                    lo_, hi_ = H[k], L[k - 2]
                                    if C[i] >= hi_ + tick and all(C[j] <= hi_ for j in range(k + 1, i)) \
                                            and (best is None or hi_ > best[1]):
                                        best = (lo_, hi_)
                                if side < 0 and L[k] > H[k - 2]:
                                    lo_, hi_ = H[k - 2], L[k]
                                    if C[i] <= lo_ - tick and all(C[j] >= lo_ for j in range(k + 1, i)) \
                                            and (best is None or lo_ < best[0]):
                                        best = (lo_, hi_)
                            fvg = best
                        if disp and fvg:
                            # ---------- PLACE_ENTRY
                            stop = s['ext'] - tick if side > 0 else s['ext'] + tick
                            if cfg.entry_mode == 'aggressive':
                                entry_px = None           # market @ next open
                                ref_entry = C[i]
                            else:
                                entry_px = fvg[1] if side > 0 else fvg[0]
                                ref_entry = entry_px
                            risk = (ref_entry - stop) * side
                            ok = cfg.min_stop_pts <= risk <= max_stop
                            t1 = t2 = None
                            if ok:
                                # opposing pools untaken as of bar i
                                if side > 0:
                                    pools = [p for (p, k_, fi) in buy if max(H[fi + 1:i + 1] or [p - 1]) < p]
                                    pools += [p for (ci, pi, p) in win_ph if ci <= i and max(H[pi + 1:i + 1] or [p - 1]) < p]
                                    pools += [p for p in ref]
                                    pools = sorted(set(p for p in pools if p > ref_entry + tick))
                                else:
                                    pools = [p for (p, k_, fi) in sell if min(L[fi + 1:i + 1] or [p + 1]) > p]
                                    pools += [p for (ci, pi, p) in win_pl if ci <= i and min(L[pi + 1:i + 1] or [p + 1]) > p]
                                    pools += [p for p in ref]
                                    pools = sorted(set(p for p in pools if p < ref_entry - tick), reverse=True)
                                need = cfg.min_rr * risk
                                if cfg.target_mode == 'nearest':
                                    if pools and abs(pools[0] - ref_entry) >= need:
                                        t1 = pools[0]; t2 = pools[1] if len(pools) > 1 else None
                                else:
                                    far = [p for p in pools if abs(p - ref_entry) >= need]
                                    if far:
                                        t1 = far[0]; t2 = far[1] if len(far) > 1 else None
                                ok = t1 is not None
                            if ok:
                                qty = min(cfg.max_contracts, int(cfg.risk_per_trade // (risk * pv)))
                                ok = qty >= 1
                            if ok:
                                order = dict(side=side, px=entry_px, stop=stop, t1=t1, t2=t2, qty=qty,
                                             placed=i, sig=i, level=s['level'], kind=s['kind'], wname=wname)
                                state = 'PLACE_ENTRY'
                            else:
                                state = 'WAIT_FOR_SWEEP'
                            setup = None
                    i += 1
                    continue

                if state == 'PLACE_ENTRY' and i > order['placed']:
                    od = order; side = od['side']
                    filled_px = None
                    if od['px'] is None:
                        filled_px = O[i] + side * slip
                        # re-validate risk at actual fill
                        risk = (filled_px - od['stop']) * side
                        if risk < cfg.min_stop_pts * 0.5 or risk > max_stop * 1.25:
                            state, order = 'WAIT_FOR_SWEEP', None; i += 1; continue
                    else:
                        # fill needs a trade-through; cancel if T1 runs first or order expires
                        if (side > 0 and L[i] <= od['px'] - tick) or (side < 0 and H[i] >= od['px'] + tick):
                            filled_px = od['px'] if not ((side > 0 and O[i] < od['px']) or (side < 0 and O[i] > od['px'])) else O[i]
                        elif (side > 0 and H[i] >= od['t1']) or (side < 0 and L[i] <= od['t1']) \
                                or i - od['placed'] > cfg.retest_bars or not in_win:
                            state, order = 'WAIT_FOR_SWEEP', None; i += 1; continue
                    if filled_px is not None:
                        attempts += 1
                        pos = Trade(side=side, window=od['wname'], day=str(d), sweep_level=od['level'],
                                    level_kind=od['kind'], signal_idx=od['sig'], entry_idx=i, entry=filled_px,
                                    stop=od['stop'], t1=od['t1'], t2=od['t2'], contracts=od['qty'],
                                    init_stop=od['stop'])
                        pos._open = od['qty']; pos._first = True
                        state, order = 'MANAGE_POSITION', None
                    else:
                        i += 1; continue

                if state == 'MANAGE_POSITION':
                    p = pos; side = p.side
                    first = p._first; p._first = False
                    stop_hit = (L[i] <= p.stop) if side > 0 else (H[i] >= p.stop)
                    if stop_hit:
                        gap_through = (O[i] < p.stop) if side > 0 else (O[i] > p.stop)
                        px = (O[i] if gap_through and not first else p.stop) - side * slip
                        p.exits.append((i, px, p._open, 'STOP' if p._open == p.contracts else 'BE_STOP'))
                        p._open = 0
                    elif not first:
                        hit1 = (H[i] >= p.t1 + tick) if side > 0 else (L[i] <= p.t1 - tick)
                        if p._open == p.contracts and hit1:
                            q = p.contracts if (p.contracts < 2 or p.t2 is None) else max(1, int(p.contracts * cfg.partial_frac))
                            p.exits.append((i, p.t1, q, 'T1')); p._open -= q
                            p.stop = p.entry           # break-even
                        elif p._open < p.contracts and p.t2 is not None and \
                                ((H[i] >= p.t2 + tick) if side > 0 else (L[i] <= p.t2 - tick)):
                            p.exits.append((i, p.t2, p._open, 'T2')); p._open = 0
                    if p._open > 0 and (i - p.entry_idx >= cfg.max_hold_bars or i == end_idx):
                        p.exits.append((i, C[i] - side * slip, p._open, 'TIME')); p._open = 0
                    if p._open == 0:
                        g = sum((px - p.entry) * side * q * pv for (_, px, q, _) in p.exits)
                        fees = 2 * fee_side * p.contracts
                        p.gross, p.pnl = g, g - fees
                        p.r_mult = p.pnl / (abs(p.entry - p.init_stop) * pv * p.contracts)
                        trades.append(p)
                        daily[d] += p.pnl
                        pos = None
                        if daily[d] <= -cfg.daily_loss_limit:
                            locked = True               # LOCKOUT until next session
                        state = 'WAIT_FOR_SWEEP'
                        if not in_win: break
                i += 1
    return trades, daily


def summarize(trades, daily, cfg=Config()):
    if not trades:
        return dict(n_trades=0)
    pnl = [t.pnl for t in trades]
    wins = [p for p in pnl if p > 0]; losses = [p for p in pnl if p <= 0]
    dvals = [daily[d] for d in sorted(daily)]
    eq, peak, mdd = 0.0, 0.0, 0.0
    for v in dvals:
        eq += v; peak = max(peak, eq); mdd = max(mdd, peak - eq)
    sd = statistics.pstdev(dvals) if len(dvals) > 1 else 0
    sharpe = statistics.mean(dvals) / sd * math.sqrt(252) if sd > 0 else 0.0
    reasons = {}
    for t in trades:
        k = '+'.join(r for (_, _, _, r) in t.exits); reasons[k] = reasons.get(k, 0) + 1
    by_win = {}
    for t in trades:
        b = by_win.setdefault(t.window, [0, 0.0]); b[0] += 1; b[1] += t.pnl
    return dict(
        n_trades=len(trades), days=len(dvals), trades_per_day=len(trades) / max(1, len(dvals)),
        win_rate=len(wins) / len(pnl), avg_win=statistics.mean(wins) if wins else 0.0,
        avg_loss=statistics.mean(losses) if losses else 0.0,
        expectancy=statistics.mean(pnl), avg_R=statistics.mean(t.r_mult for t in trades),
        profit_factor=(sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else float('inf'),
        net_pnl=sum(pnl), gross_pnl=sum(t.gross for t in trades),
        fees_and_slip=sum(t.gross for t in trades) - sum(pnl),
        max_dd_usd=mdd, max_dd_pct_acct=mdd / cfg.account, sharpe_daily=sharpe,
        exits=reasons, by_window={k: dict(n=v[0], pnl=round(v[1], 2)) for k, v in by_win.items()},
    )


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--csv', required=True)
    ap.add_argument('--tz', default='America/New_York', help='timezone of CSV timestamps (e.g. UTC, America/Chicago)')
    ap.add_argument('--instrument', default='MNQ', choices=list(INSTRUMENTS))
    ap.add_argument('--entry', default='conservative', choices=['conservative', 'aggressive'])
    ap.add_argument('--trades-out', default=None, help='write trade list CSV')
    a = ap.parse_args(argv)
    bars = load_csv(a.csv, a.tz)
    cfg = Config(instrument=a.instrument, entry_mode=a.entry)
    trades, daily = run(bars, cfg)
    print(json.dumps(summarize(trades, daily, cfg), indent=2, default=str))
    if a.trades_out:
        with open(a.trades_out, 'w', newline='') as f:
            w = csv.writer(f)
            w.writerow(['day', 'window', 'side', 'level_kind', 'sweep_level', 'entry_time', 'entry', 'stop',
                        't1', 't2', 'contracts', 'exits', 'gross', 'net', 'R'])
            for t in trades:
                w.writerow([t.day, t.window, 'LONG' if t.side > 0 else 'SHORT', t.level_kind, t.sweep_level,
                            bars.ts[t.entry_idx], t.entry, t.init_stop, t.t1, t.t2, t.contracts,
                            ';'.join(f'{r}@{px}x{q}' for (_, px, q, r) in t.exits),
                            round(t.gross, 2), round(t.pnl, 2), round(t.r_mult, 2)])


if __name__ == '__main__':
    main()
