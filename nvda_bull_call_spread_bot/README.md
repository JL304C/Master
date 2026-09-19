# NVDA Bull Call Spread Bot — Alpaca Paper Trading Automation

Automates the exact NVDA bull call debit spread strategy settled in
[`tsla_bull_call_spread/README.md`](../tsla_bull_call_spread/README.md)
after its audit/backtest iteration (Python Monte Carlo, a real-data Alpha
Vantage backtest, and a multi-year TradingView Pine Script backtest — all
three independently landed on a consistent positive edge), running it live
against an **Alpaca paper trading account**.

Strategy, unchanged from that work:
- Entry only when: 3 consecutive daily closes above the 8-day EMA, AND not
  inside the calendar week of a known/expected earnings date, AND no
  position or pending order already open.
- Construction: buy the call closest to 30 delta, sell the call whose
  strike is closest to (long strike + $10), same expiration, ~30 DTE.
  Entered as one multi-leg limit order at the net debit.
- Entry gate: debit must be ≤30% of the $10 width and ≤$3.00/share, and
  reward:risk ≥2.3x — otherwise skip the signal rather than chase price.
- Exit: close both legs together the first time the spread reaches 45% of
  the debit as profit, or when ≤2 days remain to expiration, whichever
  comes first. **No stop-loss** — this is a defined-risk spread; the audit
  found a price stop only converts recoverable trades into early partial
  losses without lowering the real (already-capped) worst case.
- Never add to, roll, or adjust either leg. One spread at a time, sized so
  a full loss of the debit is acceptable ("position for zero").

## Before running this

**Enable options trading on the Alpaca paper account.** Alpaca requires an
options trading level to be turned on even for paper accounts (Level 3 is
needed for multi-leg/spread orders) — do this in the Alpaca dashboard under
the paper account's options settings before the first run, or every order
this script submits will be rejected.

## Files

- `nvda_bull_call_spread_bot.py` — the bot. Run once per trading day.
- `requirements.txt` — Python dependency (`alpaca-py`).
- `.env.example` — copy to `.env` and fill in your **paper** keys.

## Setup on your Windows laptop

1. Install Python 3.10+ if you don't already have it (from python.org —
   check "Add python.exe to PATH" during install).
2. Open PowerShell and install the dependency:
   ```powershell
   pip install alpaca-py
   ```
3. Put `nvda_bull_call_spread_bot.py` in its own folder, e.g.
   `C:\Users\jeffl\nvda_bull_call_spread_bot\`.
4. In that same folder, create a file named `.env` (not `.env.txt` — in
   Notepad's Save As dialog, set "Save as type" to "All Files") containing:
   ```
   ALPACA_API_KEY=your_paper_key_here
   ALPACA_SECRET_KEY=your_paper_secret_here
   ```
   Use your **paper** keys (endpoint `paper-api.alpaca.markets`), never the
   live ones.
5. Test it manually first:
   ```powershell
   cd C:\Users\jeffl\nvda_bull_call_spread_bot
   python nvda_bull_call_spread_bot.py
   ```
   It should print a JSON status line and, if a signal fires and a valid
   construction is found, place a paper multi-leg order. Check
   `nvda_bull_call_spread_log.jsonl` / `nvda_bull_call_spread_trades.csv`
   (created next to the script) and your Alpaca paper dashboard to confirm.
   **Read the output closely on this first run** — see Status below.
6. Once a manual run works, schedule it in Windows Task Scheduler:
   - Trigger: daily, on a market-hours weekday time, a few minutes after
     open so the prior session's daily bar is final and quotes are live
     (e.g. 9:35 AM ET).
   - Action: `python.exe` with argument
     `C:\Users\jeffl\nvda_bull_call_spread_bot\nvda_bull_call_spread_bot.py`,
     start-in folder set to the same directory (so `.env` and the log files
     are found/written there).
   - Conditions tab → check **"Wake the computer to run this task"** if you
     want it to run even when the laptop is asleep (test this first — not
     all laptops wake reliably from Task Scheduler).
   - Settings tab → consider "Run task as soon as possible after a scheduled
     start is missed," in case the laptop is off at the scheduled time.

## Status (read before trusting this)

Built directly against Alpaca's own reference notebook for this exact
strategy (`alpacahq/alpaca-py` `examples/options/options-bull-call-spread.ipynb`)
for the multi-leg order and the two-call close pattern. Every import,
request class, enum member, and model field this script touches
(`GetOptionContractsRequest`, `OptionLatestQuoteRequest`,
`LimitOrderRequest` + `OrderClass.MLEG` + `OptionLegRequest`,
`ClosePositionRequest`, `Position.side`, `Position.avg_entry_price`,
`Order.legs`, `PositionSide.SHORT`, ...) was checked by installing
`alpaca-py` 0.44.0 and constructing each object directly against the real
SDK — not just written from memory. What that check can't cover, with no
live account reachable from the environment that wrote this: actual order
fills, real quote data shapes at runtime, and end-to-end timing. **Treat
the first manual run (step 5 above) as the real integration test.**

Delta and implied vol are computed by the script itself (bisection against
its own Black-Scholes pricer, fed with each candidate contract's real live
quote) rather than pulled from a chain-wide greeks feed — this mirrors
Alpaca's own reference implementation for this strategy, and is the same
math already validated in `tsla_bull_call_spread/nvda_final_strategy_audit.py`.

## Notes / limitations

- `EARNINGS_DATES` in the script is a manually maintained list — **update
  it every quarter** as new NVDA earnings dates are confirmed. It currently
  has 2026-08-26 (real, reported) and 2026-11-18 (expected, per the
  historical Nov-reporting pattern documented in
  `tsla_bull_call_spread/README.md`).
- The strategy's entry signal (3 days above the 8-EMA) is the full "system"
  gate as backtested — there is no separate discretionary bullish-signal
  layer on top of it. If you want to require additional confirmation before
  the bot trades, that needs to be added deliberately, not assumed.
- The script derives all state fresh from Alpaca's own account on every run
  (current positions, each option symbol's own encoded strike/expiration) —
  there is no local state file to drift out of sync with reality.
- `OI_THRESHOLD = 50` is a light liquidity filter (skip contracts with open
  interest at or below this) — adjust if it's excluding contracts you'd
  actually want, or letting through ones too thin to fill well.
- **Paper trading only.** `paper=True` is hard-coded in the script. Going
  live later means deliberately changing that, not flipping an env var.
