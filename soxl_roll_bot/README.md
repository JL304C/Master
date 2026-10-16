# SOXL Roll Bot: Alpaca paper trading

Sells weekly cash-secured puts on SOXL and **rolls instead of taking shares**. This is "Option 6" from `../soxl_put_backtest/variants.py`. That backtest ran from Sep 2021 to Oct 2026 and returned +17%/yr with a 62% worst drawdown. Buy-and-hold SOXL returned +17%/yr with a 91% drawdown. Most of the gain came in 2026, and the strategy lost about 30% in 2022.

## What it does each week
It runs on the last trading day of the week (normally Friday) between 3:30 and 3:58 PM US Eastern.

1. **The expiring put is bought back**, always:
   - **Out of the money**: buying it back costs pennies and removes after-hours assignment risk. The bot then sells a new put at the rule strike (step 2).
   - **In the money** (SOXL below the strike): this is a **roll**. The bot sells next week's put at the **same strike** with the same number of contracts.
2. **Rule strike** = SOXL price − expected move, rounded **down** to a listed strike.
   - The expected move comes from the expiration about 14 days out: price × ATM IV × √(DTE/365) × 0.798.
   - The put is sold on the expiration about 7 days out, at whatever premium is offered.
   - The bot logs whether the premium met the $83 target.
3. **Number of contracts** for a new put is ⌊min($26,000 cap, options buying power) ÷ (strike × 100)⌋, capped at 10. A roll keeps the same count.
4. **Shares from early assignment** are sold on the next roll day, and the put is rolled at the assignment price.

Orders are limit orders. Each starts at the mid price, moves halfway to the bid (or the ask when buying), then to the bid or ask, waiting 20 seconds at each step. If an order still doesn't fill, the bot logs an `alert` and stops. Before it buys back an in-the-money put, it writes `pending_roll.json`. If the replacement put doesn't sell, the next run still rolls at the same strike.

On any other day or time, the bot only logs a status line, so it's safe to schedule it every weekday.

## Setup (Windows laptop)
1. Install the dependencies: `pip install alpaca-py tzdata`. Windows Python needs `tzdata` for the Eastern-time clock.
2. Copy this folder to the laptop, for example `C:\Users\jeffl\soxl_roll_bot\`.
3. Create a `.env` file there with your **paper** keys (see `.env.example`). You can reuse the GPC bot's keys.
   - Both bots share one paper account, so the $26,000 cap applies to each bot separately. Lower `ACCOUNT_CAP` in the script if needed.
4. In the Alpaca dashboard, make sure the paper account has **options trading level 2 or higher**. Cash-secured puts need it.
5. Check the logic without placing any orders: `python soxl_roll_bot.py --dry-run --force`
6. Check the math offline: `python test_offline.py`
7. Set up Windows Task Scheduler:
   - Run it **every weekday at 3:40 PM Eastern**. Set the trigger in your laptop's local time.
   - Action: `python.exe`, argument `C:\Users\jeffl\soxl_roll_bot\soxl_roll_bot.py`, start-in set to that folder.
   - The laptop must be awake. Check "Wake the computer to run this task".

Logs go to `soxl_roll_log.jsonl` and `soxl_roll_trades.csv` next to the script.

## Limits and notes
- **Paper only.** `paper=True` is hard-coded.
- **Not yet run against Alpaca.** The cloud session that wrote this cannot reach Alpaca. Your first `--dry-run --force` on the laptop is the real test.
- **Missed run on a Friday.** If the laptop is off or asleep, an in-the-money put **will be assigned** at the close. The next roll day then sells the shares and rolls at the assignment price, but SOXL moves over the weekend change the result.
- **Quote data.** Free Alpaca accounts get the "indicative" options feed, not full OPRA quotes. Paper fills are simulated and are likely kinder than real fills.
- **Strike not listed.** If a roll's exact strike isn't listed next week, the bot uses the closest listed strike.
