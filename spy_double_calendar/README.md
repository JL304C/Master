# SPY Weekly Double Calendar — Real-Option Backtest

Tests a weekly double calendar on SPY using **real OPRA bid/ask quotes** (Databento) and real one-minute SPY bars (Alpaca).
It is a backtest only. No bot exists yet.

**Status: built and checked offline, not yet run on real data.** Databento and Cboe can't be reached from the cloud session
that wrote this, so the first real run happens on your laptop.

## The strategy as tested

| | Rule |
|---|---|
| Position | Put calendar below the price plus call calendar above it: sell the near expiry, buy the same strike at a later expiry |
| Strikes | Listed strike nearest spot ± the **expected move**, which is the mid of the at-the-money straddle at the short expiry (`--em-mult` scales it) |
| Entry | Tuesday at 10:00 AM ET, or Wednesday when Tuesday is a holiday. One trade per week per structure |
| Structures | **FF** short Friday ~10 DTE, long the next Friday · **FM** short Friday ~10 DTE, long the Monday after · **WF** short Wednesday ~8 DTE, long the Friday after (the high-VIX "Wednesday trick") · **FF2** short Friday ~10 DTE, long two Fridays later ("pad the expiry") |
| VIX filter | Prior day's VIX close < 20. An alternative also requires VIX at or below its 20-day average |
| FOMC | Skip the week if an FOMC statement day falls between entry and the time stop (`fomc_dates.txt`) |
| Profit | Resting limit orders: half the position at +20% of the debit, the rest at +30% |
| Price stop | SPY's one-minute high/low touches either strike → close everything on the next minute's quotes |
| Time stop | 3:45 PM on the last trading day ≥3 days before a Friday short expiry (the following Tuesday), or ≥2 days before a Wednesday one (the Monday) |
| Underwater, between strikes | Held to the time stop |
| Costs | $0.03 per contract per leg, open and close ($0.24 per double calendar) |

Every week is replayed regardless of VIX or FOMC. The filters are applied in the report, so filtered and unfiltered
results come from the same trades. That gives the **no-signal baseline** that exposed the Bollinger and condor results.

### Pricing: three columns

- **mid/nat** (headline): enter at the mid, exit at the natural price (sell longs at the bid, buy back shorts at the ask). Same as `bb_real_options.py`.
- **mid/mid** (optimistic): every fill at the mid.
- **nat/nat** (pessimistic): market orders both ways.

A four-legged SPY order usually fills somewhere between the mid and the natural price. If a result is only positive at mid/mid, it isn't real.

### What the report shows

- Per structure: all weeks, VIX < 20, **VIX < 20 with no FOMC (the spec)**, VIX ≥ 20, and so on. Columns: trades, win rate,
  average return on the debit, average and total $, worst trade, max drawdown, t-stat.
- **His full plan:** FF when VIX < 20, WF when VIX ≥ 20. Also "run both" on high-VIX weeks.
- VIX filters beyond "< 20": VIX in the lower/upper half of its 60-day range (his "relative to the recent baseline"),
  and the term structure: VIX9D above/below VIX, VIX at/above 0.95 × VIX3M vs contango (Cboe data, free).
- An exit-rule grid on the spec trades: the spec, a single +20/30/50% target, no profit target, no touch stop, time stop
  only, and his hold rule ("stay in while VIX is flat or higher": exit at the end of a day whose VIX close is below the
  entry VIX; that close comes ~15 minutes after the 3:45 PM check, a small look-ahead).
- **His profit curve check:** he shows ~+10% by day 3 and ~+30% after a week if SPY stays between the strikes. The
  report prints the real mid value of every trade at each day's close, split by whether a strike had been touched yet.
- Exit reasons, results by year, typical debit and strike distance, and the weeks skipped with the reason.

## Trial result (Jan 2025 – Sep 2026, $0.43 of Databento data)

| FF, mid in / natural out | Trades | Won | Total | t |
|---|---|---|---|---|
| **The spec: VIX < 20, no FOMC** | 58 | 40% | **−$637** | −1.9 |
| All weeks | 89 | 47% | −$139 | −0.3 |
| VIX ≥ 20, no FOMC | 17 | 59% | +$359 | +1.6 |

- The spec lost money even with every fill at the mid (−$288). His 85% win rate was not reproduced.
- Every structure made money only when VIX was 20 or higher. That is the opposite of his entry rule, and it rests on
  17 weeks from two selloffs (Apr 2025, Mar 2026).
- Trades that sat between the strikes until the time stop averaged −$18, not the +30% his profit curve promises.
- Removing the touch stop helped (−$386 instead of −$637). FM (Monday long leg) was the worst structure (t −4.6).

## Full result (Jan 2016 – Sep 2026, ~$3 of Databento data) — verdict: no edge

1 double calendar per trade, enter at the mid, exit at the natural price, fees included.
(Run hit a full disk mid-2023, so part of 2023 is missing; rerun after freeing space.)

| | Trades | Won | Total | t | Best case (mid/mid) | Worst case (nat/nat) |
|---|---|---|---|---|---|---|
| **FF, the spec (VIX < 20, no FOMC)** | 319 | 43% | **−$1,006** | −2.1 | +$279 | −$2,100 |
| FF, all weeks | 544 | 46% | −$1,087 | −1.4 | +$2,023 | −$3,230 |
| FF, VIX ≥ 20 | 140 | 44% | +$5 | 0.0 | +$940 | −$725 |
| FM (Monday long leg), all weeks | 392 | 37% | −$1,709 | −4.5 | +$1,023 | −$3,475 |
| WF ("Wednesday trick"), all weeks | 501 | 43% | −$1,628 | −4.0 | +$970 | −$3,508 |
| His plan: FF if VIX < 20, WF if ≥ 20 | 451 | 42% | −$1,523 | −2.8 | | |
| **FF2 (padded expiry), all weeks** | 542 | 50% | **+$809** | +0.7 | +$4,252 | −$1,345 |
| FF, time stop only (no targets, no touch stop) | 319 | 50% | +$324 | +0.5 | | |

- The spec loses. His 85% win rate is ~43% in real quotes.
- The trial's "VIX ≥ 20 wins" faded to zero over 10 years; the term-structure filters (VIX9D, VIX3M)
  are all within noise (|t| ≤ 1.2).
- FM and WF lose under every filter.
- His profit curve: trades that stayed between the strikes averaged +5% by day 3 and +4–6% by day 5–6 at the mid,
  not +10% / +30%. The touch stop (136 of 319 spec trades) and the exit spread take the rest.
- The best variants (FF2, no touch stop) are positive only at the mid and not significant. With ~20 combinations
  tried, a t of ~1 is what chance alone produces.

## Running it (on your laptop)

```
cd spy_double_calendar
pip install -r requirements.txt
copy .env.example .env        (then fill in the Alpaca and Databento keys, same as the other folders)
python test_backtest_offline.py                 # no keys needed: checks the logic on synthetic data
python spy_calendar_backtest.py --start 2025-01-01      # small trial first: check the cost and the output
python spy_calendar_backtest.py                 # full run, 2016 to two weeks ago
```

- It asks Alpaca for SPY bars and Cboe for VIX (both free), then prints a **Databento cost estimate** and waits for `y`.
- Each trade needs two small Databento requests: a 5-minute window of ~70 contracts at entry, then the 4 chosen legs
  for the holding week. The full run is ~1,600 trades (3 structures × ~540 weeks). The first run takes a while.
  Everything is cached in `cache/`, so re-runs are free and `--offline` uses the cache only.
- Options: `--structures FF` (one structure), `--em-mult 0.8` (strikes closer in), `--end 2025-12-31`, `--yes`.
- Output: `spy_calendar_report.txt` (the tables) and `spy_calendar_trades.csv` (one row per trade, including the exit grid).

## Known limits

- **Fills.** Profit targets fill exactly at their limit price once the closing value reaches it. A touch exit uses the next
  minute's quotes. Real four-leg fills can be worse in fast markets, which is when touches happen.
- **Quotes.** These are one-minute consolidated BBO snapshots, carried forward when a leg doesn't update.
- **Early expiries.** Wednesday SPY expiries start around 2016 and Monday expiries around 2018. Weeks without a listed expiry
  appear under "Weeks skipped".
- Early assignment and dividends aren't modelled. The touch stop and the exit before the short expiry make both unlikely.
- **FOMC dates** were typed in from memory. Check them against the Fed's calendar before relying on the filter.
  Other events (CPI, NFP) aren't filtered.
- His 85% win rate and 100%+ a year are self-reported. This backtest is the check on those claims.
