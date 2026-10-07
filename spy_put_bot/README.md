# SPY Weekly 10-Delta Put Bot (Alpaca paper trading)

Paper-trades the strategy tested in `../spy_put_backtest` on 13 years of real SPY option quotes.

| Rule | Setting |
|---|---|
| Entry | First trading day of each week, near the close. Missed or unfilled → retried through Wednesday |
| Option | Sell 1 SPY put, expiry closest to 90 days, delta closest to −0.10 |
| Profit target | Buy back when the put's mid is ≤ 50% of the fill price |
| Stop loss | Buy back when the mid is ≥ 3× the fill price |
| Time exit | Buy back at 21 days to expiry |
| VIX filter | None (it cut profit more than risk in the backtest) |

Backtest, 2013–2026, bid/ask fills, 1 put a week: $48,918 total, 93.1% win rate, worst drawdown −$7,466.

**Cash-secured.** Alpaca doesn't allow naked puts, so each put holds strike × 100 in cash
(~$58k with SPY near $650). About 10 puts are open at a time, so expect **~$550–650k tied up**.
`MAX_CASH_SECURED` ($750k) and `MAX_OPEN_POSITIONS` (13) cap it so other bots on the account keep their cash.

**Safe to share an account.** The bot only reads, manages and closes SPY options it opened,
as recorded in its own log `spy_put_log.jsonl` (don't delete it). It never closes, cancels or
sizes anything account-wide. `nvda_condor_bot.py` works the same way, so the two can run on the
"Backtest Delayed condor" account side by side.

**Paper only.** `paper=True` is hard-coded.

## Setup (Windows)

1. Get the code: `cd C:\Users\jeffl\Master` then `git pull`.
2. Install: `pip install -r C:\Users\jeffl\Master\spy_put_bot\requirements.txt`
3. Keys: copy the NVDA bot's keys (same account):
   ```
   copy C:\Users\jeffl\nvda_delayed_condor\.env C:\Users\jeffl\Master\spy_put_bot\.env
   ```
   Check it has `ALPACA_API_KEY=` and `ALPACA_SECRET_KEY=` lines (`notepad C:\Users\jeffl\Master\spy_put_bot\.env`).
4. Test without trading:
   ```
   cd C:\Users\jeffl\Master\spy_put_bot
   python -m pytest -q test_bot_offline.py     # logic checks, no keys needed
   python spy_put_bot.py --dry-run             # real data, decides and logs, submits nothing
   ```
   The dry run prints what it would do: on Monday–Wednesday, the put it would sell (expiry, strike, delta, price).
5. Schedule it in Task Scheduler, once per weekday at **3:45 PM Eastern** (adjust for your time zone):
   - Action: `python.exe`, argument `C:\Users\jeffl\Master\spy_put_bot\spy_put_bot.py`,
     **Start in** `C:\Users\jeffl\Master\spy_put_bot` (so it finds `.env` and its log).
   - Conditions → "Wake the computer to run this task"; Settings → "Run task as soon as possible after a scheduled start is missed".

## What it writes

- `spy_put_log.jsonl`: every decision, with prices and order IDs. **The bot's memory: keep it.**
- `spy_put_trades.csv`: the same, one line per decision, for a quick look in Notepad.

## Things to check on the first live paper fills

- The entry fills at or near the limit (mid − $0.02). If entries often expire unfilled,
  raise `ENTRY_CONCESSION` a little.
- Alpaca's delta (its own greeks) is close to −0.10 for the strike picked.
- Exits use the position's real fill price from Alpaca as the "credit".

## Differences from the backtest

- A quote with no bid, or a spread wider than max($0.50, 30% of the mid), is treated as bad:
  no exit is taken on it that day (an alert is logged) and it's re-checked on the next run.
- If two weeks pick the exact same contract, Alpaca merges them into one position at the
  average price, and the bot manages them together.

- Prices come from Alpaca's free (indicative) option feed, not OPRA; deltas are Alpaca's.
- Orders are limits near the close; an unfilled one is retried the next run, so a fill can
  be a day late. The backtest assumed every order filled at the close.
- A stop or target that's hit intraday but not at ~3:45 PM is not acted on, same as the backtest.
