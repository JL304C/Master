"""
Exit-rule study on REAL option prices (Databento OPRA), reusing bb_real_options.py's cache.

The real-price test showed the entries aren't the main problem: wins (~$45 at the 50%
take-profit) are far smaller than losses ($100-300 at the stops). This replays the SAME
entries (A+B signal and the no-signal baseline; entries are kept exactly as in
bb_real_options.py) and scores each trade under several exit rules, from the same daily
3:45 PM ET quotes:

  as tested            take profit when the MID <= 50% of the credit, paying the natural price;
                       2x stop; close below the short strike; 21 DTE  (bb_real_options.py)
  resting TP (bot)     the bot's actual take-profit: a resting GTC buy-to-close at 50% of the
                       credit, filled at that price once the natural price reaches it
  no 2x stop           resting TP, close-below-short stop, 21 DTE
  no backup stop       resting TP, 2x stop, 21 DTE
  3x stop only         resting TP, 3x stop, 21 DTE
  no stops             resting TP, 21 DTE
  TP 25% profit        resting TP at 75% of the credit, 2x stop, backup stop, 21 DTE
  hold to 7 DTE *      resting TP, 2x stop, backup stop, 7 DTE
  no stops, 7 DTE *    resting TP, 7 DTE
  no stops, expiry *   resting TP, otherwise held to expiration and settled at intrinsic value

  * need quotes after 21 DTE, which the first run didn't download. Without --download these
    rows only show trades already covered (usually none). With --download they fetch just the
    extra days for each trade (cost estimate + y/N first; cached; timeouts skip the trade).

Exits other than a resting TP pay the natural price (short ask - long bid); $0.03 per contract
per leg. Default: cache only -- no Databento calls, no cost.

Run:     python bb_exit_study.py                 (cache only, free)
         python bb_exit_study.py --download      (also fetch post-21-DTE quotes)
Options: --tickers AMD,MSFT  --only signal|baseline
"""
from __future__ import annotations

import math
import statistics
import sys
from datetime import date, timedelta

import bb_real_options as ro
from bb_stock_backtest import arg, load_env

EXITS = {
    #  name                 tp mode    tp_frac stop  backup time_dte (0 = hold to expiry)
    "as tested":           ("mid",     0.50,   2.0,  True,  21),
    "resting TP (bot)":    ("resting", 0.50,   2.0,  True,  21),
    "no 2x stop":          ("resting", 0.50,   None, True,  21),
    "no backup stop":      ("resting", 0.50,   2.0,  False, 21),
    "3x stop only":        ("resting", 0.50,   3.0,  False, 21),
    "no stops":            ("resting", 0.50,   None, False, 21),
    "TP 25% profit":       ("resting", 0.75,   2.0,  True,  21),
    "hold to 7 DTE":       ("resting", 0.50,   2.0,  True,  7),
    "no stops, 7 DTE":     ("resting", 0.50,   None, False, 7),
    "no stops, expiry":    ("resting", 0.50,   None, False, 0),
}
FEES_ROUND_TRIP = ro.FEE_PER_LEG * 4
FEES_OPEN_ONLY = ro.FEE_PER_LEG * 2


def day_rows(ctx, extension):
    """[(date, dte, raw close, (bid,ask) short or None, (bid,ask) long or None)] from the entry
    day to the 21-DTE day, then through `extension` (a {date: {sym: quote}} dict or None) up to
    the last trading day on/before expiration. The last row is flagged when it IS that day."""
    bars, raw, e, end, exp = ctx["bars"], ctx["raw"], ctx["e"], ctx["end"], ctx["exp"]
    last = end
    if extension is not None:
        while last + 1 < len(bars) and bars[last + 1]["d"] <= exp:
            last += 1
    reached_expiry = extension is not None and (bars[last]["d"] == exp or last + 1 < len(bars))
    rows = []
    for j in range(e, last + 1):
        d = bars[j]["d"]
        src = ctx["daily"] if j <= end else (extension or {})
        q = src.get(d, {})
        rows.append((d, (exp - d).days, raw[d]["c"], q.get(ctx["sym_s"]), q.get(ctx["sym_l"]),
                     reached_expiry and j == last))
    return rows


def score(ctx, rows, rule):
    """P&L (after fees) of one trade under one exit rule, or None if the data runs out first."""
    tp_mode, tp_frac, stop, backup, time_dte = rule
    credit, short, long = ctx["credit"], ctx["short"], ctx["long"]
    width = short - long
    target = round(tp_frac * credit, 2)
    last_q = None
    for d, dte, close, qs, ql, expiry_day in rows:
        mid = natural = None
        if qs and ql:
            mid = round((qs[0] + qs[1]) / 2 - (ql[0] + ql[1]) / 2, 2)   # as bb_rules.exit_reason sees it
            natural = min(width, max(0.0, qs[1] - ql[0]))
            last_q = (mid, natural)

        def pay_natural():
            if natural is not None:
                return natural
            if last_q:
                return last_q[1]
            return min(width, max(0.0, short - close) - max(0.0, long - close) + 0.10)

        if stop is not None and mid is not None and mid >= stop * credit:
            return (credit - pay_natural()) * 100 - FEES_ROUND_TRIP, "stop"
        if backup and close < short:
            return (credit - pay_natural()) * 100 - FEES_ROUND_TRIP, "backup"
        if tp_mode == "resting" and natural is not None and natural <= target:
            return (credit - target) * 100 - FEES_ROUND_TRIP, "take_profit"
        if tp_mode == "mid" and mid is not None and mid <= tp_frac * credit:
            return (credit - pay_natural()) * 100 - FEES_ROUND_TRIP, "take_profit"
        if time_dte and dte <= time_dte:
            return (credit - pay_natural()) * 100 - FEES_ROUND_TRIP, "time"
        if time_dte == 0 and expiry_day:                  # held to the end: settles at intrinsic value
            settle = min(width, max(0.0, short - close) - max(0.0, long - close))
            return (credit - settle) * 100 - FEES_OPEN_ONLY, "expiry"
    return None


def summary(pnls):
    if not pnls:
        return None
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    sd = statistics.pstdev(pnls) if len(pnls) > 1 else 0
    return dict(n=len(pnls), won=len(wins) / len(pnls), total=sum(pnls),
                avg_win=statistics.mean(wins) if wins else 0, avg_loss=statistics.mean(losses) if losses else 0,
                worst=min(pnls), t=statistics.mean(pnls) / (sd / math.sqrt(len(pnls))) if sd else 0.0)


def main():
    load_env()
    import os
    download = "--download" in sys.argv
    client = None
    if download:
        key = os.environ.get("DATABENTO_API_KEY")
        if not key:
            sys.exit("Missing DATABENTO_API_KEY in .env")
        import databento as db
        client = db.Historical(key)
    quotes = ro.DatabentoQuotes(client, offline=not download)
    tickers = [x.strip().upper() for x in (arg("--tickers") or ",".join(ro.DEFAULT_TICKERS)).split(",") if x.strip()]
    modes = [arg("--only")] if arg("--only") else ["signal", "baseline"]
    start = date.fromisoformat(arg("--start", "2016-01-01"))

    # 1) replay the entries from the cache, collecting each trade's daily quotes
    collected = []
    for tk in tickers:
        try:
            bars, raw = ro.alpaca_bars(tk, start - timedelta(days=200))
            earn = ro.load_earnings(tk)
        except Exception as exc:                          # noqa: BLE001
            print(f"{tk}: SKIPPED -- {exc}")
            continue
        for mode in modes:
            quotes.offline = True                        # entries and 21-DTE windows: cache only
            ro.replay(tk, bars, raw, earn, mode, quotes, start, lambda msg: None,
                      on_window=lambda t, ctx: collected.append((t, ctx)))
        print(f"{tk}: {sum(1 for t, _ in collected if t['ticker'] == tk)} cached trades")

    # 2) quotes after 21 DTE for the long-hold rules
    ext = {}
    needs = [(t, c) for t, c in collected if c["end"] + 1 < len(c["bars"]) and c["bars"][c["end"] + 1]["d"] <= c["exp"]]
    if download and needs:
        quotes.offline = False
        print(f"\n{len(needs)} trades need quotes after 21 DTE (one small download each, ~2-3 weeks of 2 contracts).")
        if input("Download? [y/N] ").strip().lower() == "y":
            for k, (t, c) in enumerate(needs, 1):
                d0 = c["bars"][c["end"] + 1]["d"]
                last = c["end"]
                while last + 1 < len(c["bars"]) and c["bars"][last + 1]["d"] <= c["exp"]:
                    last += 1
                got = quotes.window([c["sym_s"], c["sym_l"]], d0, c["bars"][last]["d"])
                if got is not None:
                    ext[id(c)] = got
                if k % 25 == 0:
                    print(f"  {k}/{len(needs)}", flush=True)
    else:
        quotes.offline = True
        for t, c in needs:                               # use any extension already cached
            d0 = c["bars"][c["end"] + 1]["d"]
            last = c["end"]
            while last + 1 < len(c["bars"]) and c["bars"][last + 1]["d"] <= c["exp"]:
                last += 1
            got = quotes.window([c["sym_s"], c["sym_l"]], d0, c["bars"][last]["d"])
            if got is not None:
                ext[id(c)] = got

    # 3) score every trade under every rule
    print("\nREAL option prices, same entries, different exits. 1 contract; fees included.")
    print("Long-hold rules (*) only count trades whose post-21-DTE quotes are available.\n")
    for mode in modes:
        trades = [(t, c) for t, c in collected if t["mode"] == mode]
        big = {"AMZN": date(2022, 6, 6), "GOOGL": date(2022, 7, 18)}   # 20:1 splits; before them strikes > $1,000
        print(f"=== {mode.upper()} ({'A+B, the bot' if mode == 'signal' else 'no signal, weekly'}): "
              f"{len(trades)} entries ===")
        print(f"{'exit rule':<21}{'trades':>7}{'won':>6}{'total':>10}{'avg win':>9}{'avg loss':>10}"
              f"{'worst':>8}{'t':>6}{'ex AMZN/GOOGL':>15}")
        for name, rule in EXITS.items():
            long_hold = rule[4] < 21
            pnls, pnls_small = [], []
            for t, c in trades:
                if long_hold and id(c) not in ext:
                    continue
                r = score(c, day_rows(c, ext.get(id(c)) if long_hold else None), rule)
                if r is None:
                    continue
                pnls.append(r[0])
                if t["ticker"] not in big or t["entry"] >= big[t["ticker"]]:
                    pnls_small.append(r[0])
            s = summary(pnls)
            label = name + (" *" if long_hold else "")
            if not s:
                print(f"{label:<21}{'no trades with data':>30}")
                continue
            print(f"{label:<21}{s['n']:>7}{s['won']:>6.0%}{s['total']:>10,.0f}{s['avg_win']:>9,.0f}"
                  f"{s['avg_loss']:>10,.0f}{s['worst']:>8,.0f}{s['t']:>6.1f}{sum(pnls_small):>15,.0f}")
        print()


if __name__ == "__main__":
    main()
