# GPC Wheel Bot — Alpaca Paper Trading Automation

Automates the exact cash-secured-put / covered-call wheel strategy that was
stress-tested in `gpc_wheel_audit.py` (Black-Scholes + Monte Carlo simulation
against real GPC price history), running it live against an **Alpaca paper
trading account**.

Strategy, unchanged from the audit:
- Sell 1 cash-secured put on GPC, ~30 DTE, ~20% OTM — only if the strike
  fits under a fixed **$26,000 account cap** (not the paper account's real
  buying power — this is the cap the user intends to trade live with later).
- If assigned: hold the 100 shares, sell 1 covered call, ~30 DTE, ~10% OTM.
- At ≤5 days to expiration, if the call is still OTM, roll it: buy to close,
  sell a new ~30 DTE call, strike = max(10% OTM from current price, cost
  basis) — the cost-basis floor, so the strategy never voluntarily locks in
  a loss on the shares.
- Once shares are called away, restart with a new put.

The script derives all state fresh from Alpaca's own account on every run
(current positions, each option symbol's own encoded strike/expiration) —
there is no local state file to drift out of sync with reality.

**Paper trading only.** `paper=True` is hard-coded in the script. Going live
later means deliberately changing that, not flipping an env var.

## Files

- `gpc_wheel_bot.py` — the bot. Run once per trading day.
- `gpc_wheel_audit.py` — the audit/backtest that validated the strategy
  before this bot was written (kept for reference/re-validation).
- `requirements.txt` — Python dependency (`alpaca-py`).
- `.env.example` — copy to `.env` and fill in your **paper** keys.

## Setup on your Windows laptop

1. Install Python 3.10+ if you don't already have it (from python.org —
   check "Add python.exe to PATH" during install).
2. Open PowerShell and install the dependency:
   ```powershell
   pip install alpaca-py
   ```
3. Put `gpc_wheel_bot.py` in its own folder, e.g.
   `C:\Users\jeffl\gpc_wheel_bot\`.
4. In that same folder, create a file named `.env` (not `.env.txt` —
   in Notepad's Save As dialog, set "Save as type" to "All Files") containing:
   ```
   ALPACA_API_KEY=your_paper_key_here
   ALPACA_SECRET_KEY=your_paper_secret_here
   ```
   Use your **paper** keys (endpoint `paper-api.alpaca.markets`), never the
   live ones.
5. Test it manually first:
   ```powershell
   cd C:\Users\jeffl\gpc_wheel_bot
   python gpc_wheel_bot.py
   ```
   It should print a JSON status line and, if conditions are met, place a
   paper order. Check `gpc_wheel_log.jsonl` / `gpc_wheel_trades.csv` (created
   next to the script) and your Alpaca paper dashboard to confirm.
6. Once a manual run works, schedule it in Windows Task Scheduler:
   - Trigger: daily, on a market-hours weekday time (e.g. 9:35 AM ET, a few
     minutes after open so quotes are live).
   - Action: `python.exe` with argument
     `C:\Users\jeffl\gpc_wheel_bot\gpc_wheel_bot.py`, start-in folder set to
     the same directory (so `.env` and the log files are found/written there).
   - Conditions tab → check **"Wake the computer to run this task"** if you
     want it to run even when the laptop is asleep (test this first — not
     all laptops wake reliably from Task Scheduler).
   - Settings tab → consider "Run task as soon as possible after a scheduled
     start is missed," in case the laptop is off at the scheduled time.

## Notes / limitations

- `STRIKE_INCREMENT` is set to $5, a guess for GPC's real option chain —
  adjust in the script if Alpaca's actual chain uses a different increment.
- The strategy assumes assignment/expiration is handled by Alpaca/OCC
  automatically; the script does not itself submit exercise/assignment
  instructions, only opens/closes/rolls the short option leg.
- Not yet run end-to-end against the live Alpaca paper API — the first
  manual run (step 5 above) is the real test of the order-submission logic.
