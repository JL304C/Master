# SPY Weekly 10-Delta Short Put: Real-Quote Backtest

Tests a YouTuber's claim for this strategy: about 220 trades over the last 5 years, a 97.7% win rate and about $141 average P&L per trade.
The backtest uses **real OPRA bid/ask quotes from Databento**.

**Status: built and checked offline, not yet run on real data.** The cloud session that wrote this can't reach Databento,
so the first real run happens on your laptop (same as `spy_double_calendar`).

## The strategy as tested

| | Rule |
|---|---|
| Entry | Every Monday at the close, or the next trading day when Monday is a holiday |
| Expiry | The listed expiration closest to 90 calendar days (monthly, weekly or quarterly, whichever is listed) |
| Strike | The put whose delta is closest to −0.10 |
| Size | 1 contract per entry. A new one every week regardless of open positions (~10 overlapping) |
| Profit target | Buy back at the first close where the put is ≤ 50% of the credit |
| Time exit | Buy back at the first close at ≤ 21 days to expiry |
| Stop loss | None by default (as in the source). `--stop-loss 3` closes at the first close where the put is ≥ 3× the credit (≈ 2× credit loss); `--stop-loss none,2,3,4` compares levels |
| Commission | $1.00 per contract per side (`--commission`) |

Two fill models run on **the same contracts**:
- **mid**: sell at the mid, buy back at the mid, mark open positions at the mid.
- **conservative**: sell at the bid, buy back at the ask, mark open positions at the ask.

## Where the numbers come from

- **Quotes**: Databento `OPRA.PILLAR`, schema `cbbo-1m` (consolidated NBBO in 1-minute bars). History starts **2013-04-01**,
  so **2008 is not covered**; 2015, 2018 (Feb vol spike), 2020 (COVID) and 2022 are. A contract's closing quote is its last
  record in the minutes before the 4:00 PM close (1:00 PM on early-close days). `cbbo-1m` only writes a record when the
  quote changes, so the window is a few minutes wide.
- **Delta**: Databento has no greeks, so they're computed here. For each expiry, the forward price and discount factor
  are fitted from put-call parity on near-the-money strikes. Then each put's implied vol (Black-76) is solved from its mid,
  and spot delta = −e^(−qT)·N(−d1). No outside interest-rate or dividend inputs are used. SPY options are American; the
  early-exercise value of a 10-delta put is negligible.
- **SPY price**: on entry days, from the chain's parity fit. On other days, from 5 call/put pairs within 3% of the money
  (`K + C − P`). It's used for buying power and the trade log only.
- **Buying power** (Reg T naked put), per position, recomputed daily with the current mid:
  `max(20% × SPY − OTM amount + premium, 10% × strike + premium) × 100`. The report shows the peak total across all open positions.

### What gets downloaded (cost and disk)

Same approach as `spy_double_calendar`: only what's needed, everything cached in `cache/`.

- **Entry days** (~700 since 2013): the whole SPY chain over the last 3 minutes before the close. Only the usable slice is
  saved: puts 40% below to 3% above spot at 45–150 days out, and near-the-money calls/puts for the parity fits.
- **Every other day** (~3,400): one small request for the ~10 open contracts plus the 10 parity contracts.

On disk this should stay well under 100 MB. Before downloading anything, the script prints **Databento's cost estimate**
(sampled from real days) and waits for `y`. Re-runs are free, and `--offline` never downloads.
It's about 4,000 small requests, so the first full run takes a while. It can be stopped and restarted, and it picks up
where it left off.

## Running it (on your laptop)

```
cd spy_put_backtest
pip install -r requirements.txt
python -m pytest -q tests                        # no key needed: checks the logic on synthetic data
python backtest.py --start 2025-01-01            # small trial first: check the cost and the output
python backtest.py                               # full run, 2013-04-01 to two trading days ago
```

- The key is read from `DATABENTO_API_KEY` in a `.env` in this folder, or in any sibling folder (`spy_double_calendar/.env` works).
- Stop-loss comparison on the data you already have (no downloads): `python backtest.py --offline --modes mid --stop-loss none,2,3,4`.
  Each level gets its own folder (`results/mid_stop3x/` …) and a row in `results/report.txt`.
- Options: `--commission 0.65`, `--start/--end`, `--modes mid` (one fill model), `--offline`, `--yes` (skip the cost prompt),
  `--data-dir <folder>` (local full-chain parquet files from another vendor instead of Databento).

## Output (`results/`)

| File | What |
|---|---|
| `report.txt` | Summary table: full period and last 5 years, both fill models, next to the source claim |
| `<mode>/trades.csv` | Entry date, strike, expiry, entry DTE/delta/IV/spot, credit, exit date, exit reason, exit price, P&L |
| `<mode>/summary.json` | Win rate, avg win/loss, largest loss, total P&L, expectancy, profit factor, max drawdown ($ and % of peak buying power), peak buying power, max concurrent positions. Full period and last 5 years |
| `<mode>/by_year.csv` | Per calendar year (by exit date): trades, win rate, P&L, worst trade, mark-to-market change, max drawdown within the year |
| `<mode>/equity_daily.csv` | Daily mark-to-market equity, realized/unrealized, open positions, buying power, SPY |
| `<mode>/equity_curve.png`, `drawdown.png`, `pnl_histogram.png` | Charts |
| `<mode>/skipped_entries.csv` | Weeks with no entry, and why |

## Known limits

- **End-of-day only.** The profit target is checked at each close and filled at that close's price. A put that touches
  50% intraday and bounces back isn't taken, and a close well below 50% fills at that better price.
- **Stops are checked at the close.** A put that gaps through its stop overnight is closed at that close's (worse) price; an intraday spike that reverses by the close doesn't trigger it.
- **Missing quotes.** If a contract has no quote on a day, its last mark is carried forward (`stale_marks` in
  `equity_daily.csv`). If it has no quote on the 21-DTE day, it's closed at the next close that has one (noted in the trade log).
- **Data quality.** Databento builds pre-2023-02-28 `cbbo-1m` from subsampled data, so very fast closes may be slightly stale.
- **Early assignment and dividends** aren't modelled. Both are very unlikely on a 10-delta put closed at 21 DTE.
- **"Last 5 years"** means entries in the 5 years before the last day of data. The source's exact window is unknown.
