# Bollinger Oversold Put Credit Spreads (Alpaca paper trading)

This bot sells a put credit spread about 1% of the stock price wide when a stock closes below its lower Bollinger band. It works like the NVDA
delayed iron condor bot: it trades the same **Alpaca paper account**, `paper=True` is hard-coded, it logs every decision,
and it has a `--dry-run` mode. It only touches option contracts it opened itself, which it records in `bb_state.json`.
That means it can share the account with the condor bot and the other bots.

**Not backtested yet.** The condor strategy was backtested before its bot was written. This strategy has not been.
Treat the paper results as the first test.

> **Current settings: backtest variant A+B, 9 tickers** (chosen 2026-09-29 from the 11-ticker stock-side backtest).
> - **A:** the signal is a close back *above* the lower band the day after an oversold close.
> - **B:** the short strike is 5% below the lower of the POC and the band.
> - **Expirations:** standard monthlies 45–90 DTE, as originally specified. Variant D (30–60 DTE with weeklies) lost
>   money in every combination.
> - **Watchlist:** AMD, MSFT (Technology); GOOGL, META (Communication); AMZN; UNH; CAT; COST; SPY.
>   - NVDA is left out because the NVDA bull call spread bot treats every NVDA option on the account as its own.
>   - XOM and JPM are left out because their credits rarely reached $0.50.
>   - When more signals arrive than the limits allow, the tickers earlier in `WATCHLIST` go first.
>
> **Pooled model result for A+B over 11 tickers, 2016–2026:** 93 trades, 71% won, +$1,505, t = 1.7, positive at all
> three IV levels. But the no-signal baseline, entering the same kind of spread whenever flat, made +$6,259 in the same
> model. So the profit comes mostly from put premium and the trade management, not from the Bollinger timing. This is
> paper-trading only until real option prices (Databento OPRA) confirm it.
>
> The original rules are one switch away: `ENTRY_SIGNAL = "cross_below"`, `STRIKE_OFFSET = 0.0`.

> **Real option prices (Databento OPRA, run 2026-09-30), 9 tickers, 2016–Sep 2026, 1 contract:**
>
> | | Trades | Won | Total (exits at the natural price) | t | Total if every exit filled at the mid |
> |---|---|---|---|---|---|
> | **A+B (the bot)** | 75 | 64% | **−$3,018** | −1.7 | −$491 |
> | No-signal baseline, weekly | 526 | 70% | −$8,923 | −2.2 | +$5,366 |
>
> - A+B lost money on 8 of 9 tickers; AMD made +$37.
> - A+B loses even in the optimistic case where every exit fills at the mid.
> - The model had estimated +$1,505 for A+B.
> - Wins average about +$45 (the 50% take-profit) and losses −$100 to −$300 (the 2× stop and the backup stop).
> - Paying the bid/ask on exits removes what little edge the mid prices show.
> - Pre-split AMZN and GOOGL (strikes over $1,000) account for the largest losses. Without them, A+B is still −$605
>   over 62 trades.
> - Some trades are missing because Databento timed out: 4 signal trades and 48 baseline trades.
>
> **Verdict:** the strategy has no edge with real prices. Paper-trading it only confirms a losing rule set.

## Rules (as originally specified)

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

`python test_bot_offline.py` runs 64 checks with no network and no keys. It checks the rules directly (bands, cross, POC,
strike pick, monthly/earnings filter, exits, limits, earnings CSV). It then runs the bot against fake Alpaca and
Alpha Vantage data through:
- signal → next-morning order;
- fill → resting take-profit;
- a stop, which cancels the take-profit first, then the close fill and the P&L row;
- take profit, a resting take-profit fill, and the time stop;
- skips for earnings, an Alpha Vantage failure, low credit, a stale signal, an unfilled entry and the per-ticker limit;
- dry run, `--force-entry`, and `--ignore-earnings` (including its refusal without `--dry-run`).

All pass, and every order passes alpaca-py's own request validation.

## Stock-side backtest (`bb_stock_backtest.py`)

Replays the bot's own rules (`bb_rules.py`) on free daily bars, from Alpaca's SIP feed since 2016. It uses AMD's real
earnings dates from Alpha Vantage `EARNINGS` and `EARNINGS_CALENDAR`, which costs 2 requests, cached for the day.

Run: `python bb_stock_backtest.py` (options: `--symbol AMD --start 2016-01-01`). It uses the same `.env` as the bot and
writes `bb_backtest_trades.csv`.

Several tickers in one run: `python bb_stock_backtest.py --symbols AMD,MSFT,GOOGL,META,AMZN,JPM,XOM,UNH,CAT,COST,SPY`
- It prints a pooled table of every variant across all tickers, plus per-ticker rows for A+B+D and the original rules.
- Each new ticker costs 2 Alpha Vantage requests (the free limit is 25 a day). The results are cached for the day,
  and the requests are spaced about 13 seconds apart, so 10 new tickers take about 5 minutes.
- A ticker that fails, for example after the daily limit is hit, is skipped with a message. ETFs such as SPY have no
  earnings, so no earnings filter applies to them.

- **Stock-side, no option prices:**
  - how often the signal fires;
  - how many signals survive the earnings, expiration and one-per-ticker rules;
  - how far below the entry price the short strike sits;
  - how often the stock closed below the short strike before the 21 DTE time stop.
- **Model P&L:** the spread is priced with Black-Scholes (IV = 20-day realized vol × 0.8 / 1.0 / 1.2, plus a put skew),
  and the $0.50 minimum credit and all four exits are applied. It is a model: on the NVDA condor the same kind of
  model overstated real credits by about 80%. Real prices need Databento OPRA.
- **Variants:** it runs all 8 combinations of three changes to the rules:
  - **A:** enter when the close gets back above the lower band;
  - **B:** short strike 5% below min(POC, band);
  - **D:** 30–60 DTE, weekly expirations allowed.

  It prints one comparison table, plus the trade lists for A+B+D and for the best variant.
- **Baseline:** the same rules entered whenever flat, with the short strike the same distance below the price. This
  shows whether the signal adds anything.
- **Approximations:**
  - Listed strikes are approximated. The spacing is $2.50 under $100, $5 under $300 and $10 above.
  - Exits are checked at each day's close; the resting take-profit is assumed to fill at exactly 50%.
  - Stop and time exits pay the mid plus $0.10.

## Real option prices (`bb_real_options.py`, Databento OPRA)

This replays the bot's current rules (A+B) with **real bid/ask quotes**, next to a no-signal baseline. The baseline
enters on the last trading day of each week when flat, with the short strike about 8.4% below the close.

- **Entries:** the real listed chain at 9:45 AM the morning after the signal decides the expiration, the strikes and the
  mid credit. The fill is assumed at the mid, which is the bot's limit price.
- **Exits:** checked each day with the real 3:45 PM quotes, using the bot's exit rules. Each exit pays the natural price,
  like the bot's closing order. `total@mid` shows the optimistic case where exits fill at the mid.
- **Data used:**
  - Alpaca daily bars: split-adjusted for the signal, raw for strikes.
  - Earnings: the backtest's cached Alpha Vantage files.
  - Databento `OPRA.PILLAR` `cbbo-1m`: the whole put chain for 3 minutes on each entry morning, plus the two
    contracts over each trade's life.
  - Mornings the earnings rule already blocks are never downloaded. A trade that spans a stock split is skipped.
- **Cost:** it prints Databento's cost estimate and waits for `y`. Downloads are cached in `opra_cache/`, so re-runs
  are free.
- **Setup:** `pip install databento pandas`, then add `DATABENTO_API_KEY` to `.env`.
- **Run:** `python bb_real_options.py` (options: `--tickers AMD,MSFT`, `--only signal|baseline`, `--start 2016-01-01`).
  It writes `bb_real_option_trades.csv`.
- **Offline test:** `python test_real_options_offline.py`, with no network and no keys.

## Exit-rule study (`bb_exit_study.py`)

This keeps the **same entries** as `bb_real_options.py` (A+B signal and the no-signal baseline) and scores each trade
under different exit rules, using the same real 3:45 PM quotes.

- **Rules tested:**
  - the rules as tested;
  - the bot's resting 50% take-profit, filled at its limit once the natural price reaches it;
  - no 2× stop;
  - no backup stop;
  - a 3× stop only;
  - no stops at all;
  - a 25%-profit take-profit;
  - holding to 7 DTE, with or without stops;
  - holding to expiry.
- **Check against the first run:** the "as tested" row reproduces `bb_real_options.py`'s P&L trade for trade.
- **Downloads:** by default it runs from the cache only, with no Databento calls. `--download` also fetches the
  post-21-DTE quotes that the long-hold rules need. It asks y/N first and caches what it gets.
- **Run:** `python bb_exit_study.py` or `python bb_exit_study.py --download`.
- **Offline test:** `python test_exit_study_offline.py`.
