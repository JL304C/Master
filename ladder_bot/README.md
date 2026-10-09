# 1-1-1-2 Put Step-Down Ladder (DT Options) — backtest + Alpaca paper bot

Read **`BACKTEST_REPORT.md` first.** The bot will not place orders until you
approve those results by setting `LADDER_BACKTEST_APPROVED=yes` in `.env`.

## Files

- `ladder_common.py` — strike selection (delta, then equal spacing), ladder
  payoff/max-profit/breakeven/drop-loss math, BP requirements. Shared by both
  scripts so the bot trades exactly what was backtested.
- `ladder_backtest.py` — 2008–present SPY backtest; hold / stop variants;
  `--skew`, `--slip`, `--no-credit-filter`, `--bp-cap` flags.
- `ladder_bot.py` — Alpaca **paper** bot (`paper=True` hard-coded).
- `data/` — SPY weekly OHLC (Alpha Vantage) and VIX daily (CBOE).
- `results/` — base-case trades, skipped Fridays, summary.
  `results_sens/` — summaries of the sensitivity runs.

## Bot behavior

- **Fridays:** builds the ladder from Alpaca's live SPY put chain.
  - Expiration: the closest monthly to 90 DTE.
  - Strikes: picked by delta (22.5/17/13/10), then snapped to equal spacing.
- **Entry rules:** opens only if all of these hold:
  - the ladder is a net credit at mid;
  - strategy BP in use plus the new ladder stays ≤ `LADDER_BP_CAP_PCT` of equity (default 20);
  - options BP and Level 3 are available;
  - no SPY option order is still working.
- **Order:** one **mleg** limit order, legs 1/1/1/2, with the limit at the mid
  credit (Alpaca: negative limit = credit). Never sent as separate legs.
- **`LADDER_MODE=stop`:** every run, closes any ladder whose breakeven SPY is
  below, using one mleg market order.
- **Logging:**
  - every check, would-open, open, rejection and stop goes to `ladder_log.jsonl` and `ladder_trades.csv`;
  - opened ladders go to `ladder_ledger.jsonl`.

Known risk: Alpaca rejects mleg orders containing an uncovered short leg, and
this ladder has one. If that happens, the bot logs `open_rejected` with
Alpaca's message and does nothing else.

## XSP instead of SPY

`python ladder_bot.py --force-day --underlying XSP` (or `LADDER_UNDERLYING=XSP`
in `.env`) builds the same ladder on XSP, the Mini-SPX index (SPX/10):
cash-settled, European-style, so no early assignment and no shares at expiry.
Alpaca has no quote for the index itself, so the bot infers XSP's level from
put-call parity on XSP's own options. The buying-power cap counts SPY and XSP
ladders together; `--bp-cap-pct` overrides the cap for one run.

## Setup

```
pip install -r requirements.txt
copy .env.example .env      # fill in PAPER keys; leave LADDER_BACKTEST_APPROVED=no
python ladder_bot.py --force-day     # dry run: prints today's ladder + the order it would send
```

Schedule it daily (Task Scheduler, ~3:30 PM ET) the same way as the GPC bot.
