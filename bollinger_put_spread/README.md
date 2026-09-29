# Bollinger Oversold Put Credit Spreads (Alpaca paper trading)

This bot sells a put credit spread about 1% of the stock price wide when a stock closes below its lower Bollinger band. It works like the NVDA
delayed iron condor bot: it trades the same **Alpaca paper account**, `paper=True` is hard-coded, it logs every decision,
and it has a `--dry-run` mode. It only touches option contracts it opened itself, which it records in `bb_state.json`.
That means it can share the account with the condor bot and the other bots.

**Not backtested yet.** The condor strategy was backtested before its bot was written. This strategy has not been.
Treat the paper results as the first test.

## Rules

| Step | Rule |
|---|---|
| Watchlist | `WATCHLIST = {"AMD": "Technology"}` at the top of `bb_put_spread_bot.py`. Add tickers as `"TICKER": "Sector"`. The sector is used for the max-2-per-sector limit. |
| Signal (after the close) | Uses daily closes and a 50-day SMA ± 2 standard deviations. A signal fires when today's close is below the lower band and yesterday's close was at or above yesterday's lower band. |
| Order timing | The order goes in the next trading morning. A signal that isn't acted on that morning is logged as stale and skipped. |
| POC | Uses the last 126 daily bars. Each bar's volume is spread evenly across its high–low range, split into 100 equal price bins. The POC is the middle of the bin with the most volume. |
| Short put | The highest listed strike strictly below both the POC and the lower band, both taken from the signal day. |
| Long put | The target width is 1% of the signal-day close (`WIDTH_PCT` in `bb_rules.py`), e.g. $6.08 on AMD at $607.87. The long put is the listed strike below the short that is closest to short − target; on a tie the narrower spread wins. When strikes are spaced wider than the target, it is simply the next strike down, e.g. AMD 400/390 in $10 steps. The width actually used and the maximum loss are logged. |
| Expiration | A standard monthly (3rd Friday, or the Thursday before when that Friday is a holiday) 45–90 DTE out. It must expire before the next earnings date from Alpha Vantage `EARNINGS_CALENDAR`. If several qualify, the nearest one is used. If none qualify, the trade is skipped. |
| Order | One multi-leg DAY limit order at the mid credit for 1 contract. It is skipped if the mid credit is under $0.50. |
| Limits | At most 5 open spreads in total, 2 per sector, and 1 per ticker. |
| Exits (near the close, at mid prices) | Checked in this order: **stop-loss** when the spread value is ≥ 2× the entry credit; **backup stop** when the underlying is below the short strike; **take profit** when the spread value is ≤ 50% of the entry credit; **time stop** at ≤ 21 DTE. |
| Resting take-profit | Once the entry fills, the bot also leaves a GTC buy-to-close order at 50% of the credit (`RESTING_TP_ORDER = True`). Before any other exit, it cancels that order and confirms the cancel. |

## Three scheduled runs (weekdays, Task Scheduler)

| Time (ET) | Command | What it does |
|---|---|---|
| ~4:30 PM | `python bb_put_spread_bot.py --signal` | Checks each ticker's completed daily bar for a signal and saves it for the morning. |
| ~9:45 AM | `python bb_put_spread_bot.py --enter` | Picks the expiration and strikes, prices the spread and places the order for each pending signal. |
| ~3:45 PM | `python bb_put_spread_bot.py --manage` | Checks the exits for each open spread and submits closing orders. |

Every run first checks with Alpaca how its tracked orders went:
- A filled entry becomes an open spread, using the real fill credit.
- An entry that expired unfilled is logged in `bb_signals.csv` as `entry_not_filled`.
- A filled exit is written to `bb_trades.csv`.
- An exit that went unfilled is retried at the next `--manage` run.

Exit orders are limit orders at the natural price (short ask − long bid), so they fill. The exit trigger itself uses mids.

## Logs

- `bb_signals.csv`: one row for every signal, with the close, band, SMA, POC, what happened (`pending`, `order_placed`,
  `skipped`, `entry_not_filled`) and the reason. When the bot gets that far, it also records the expiration, strikes and mid credit.
- `bb_trades.csv`: one row per closed trade, with the signal, entry and exit dates, expiration, strikes, qty, entry credit,
  exit price, P&L and exit reason.
- `bb_log.jsonl`: every decision, including days with no signal and each exit check.
- `bb_state.json`: the spreads the bot tracks and its pending signals. **Don't delete it.** It is how the bot recognises
  its own positions.

## Setup (Windows, same as the condor bot)

1. Run `pip install alpaca-py`.
2. Copy this folder to its own folder, e.g. `C:\Users\jeffl\bollinger_put_spread\`. The bot needs `bb_rules.py` beside it.
3. Create `.env` in that folder with the **same paper keys as the condor bot**, plus a free Alpha Vantage key (see
   `.env.example`). The paper account already has options level 3, which spreads need.
4. Test it without trading:
   - `python bb_put_spread_bot.py --signal --dry-run`
   - `python bb_put_spread_bot.py --enter --dry-run --force-entry AMD` runs the whole entry process for AMD today, as if
     it had signalled, and logs the strikes and credit it would use. It submits nothing and saves no state.
   - Add `--ignore-earnings` to that command to switch the earnings filter off, so you can see the strikes and credit
     even when earnings block every expiration. The bot refuses this flag without `--dry-run`, and the log line is
     marked `[TEST: earnings ignored]`.
5. Schedule the three commands above, with "Start in" set to the folder.

## Data notes

- Daily bars come from Alpaca's **SIP** feed. The free plan allows SIP data up to 15 minutes old, which is why `--signal`
  runs after the close. If SIP is refused, the bot falls back to IEX, and IEX volume is only a small part of the total.
  The feed used is logged.
- The backup stop uses the latest price at the ~3:45 PM run in place of the official close.
- Alpha Vantage's free tier allows about 25 requests a day. Earnings are only fetched when an entry is being considered,
  and are cached for the day. If the request fails or returns no data, the trade is skipped rather than risk holding
  through earnings.
- **Not yet verified against the live API:** whether Alpaca accepts a **GTC** multi-leg options order. If it rejects the
  resting take-profit, the bot logs an alert and the daily `--manage` check still takes profit at 50%. Everything else
  uses the same order format the condor bot already confirmed on a real paper order (a negative limit price means a credit).

## Tests

`python test_bot_offline.py` runs 59 checks with no network and no keys. It checks the rules directly (bands, cross, POC,
strike pick, monthly/earnings filter, exits, limits, earnings CSV). It then runs the bot against fake Alpaca and
Alpha Vantage data through:
- signal → next-morning order;
- fill → resting take-profit;
- a stop, which cancels the take-profit first, then the close fill and the P&L row;
- take profit, a resting take-profit fill, and the time stop;
- skips for earnings, an Alpha Vantage failure, low credit, a stale signal, an unfilled entry and the per-ticker limit;
- dry run, `--force-entry`, and `--ignore-earnings` (including its refusal without `--dry-run`).

All pass, and every order passes alpaca-py's own request validation.
