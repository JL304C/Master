# NQ/MNQ Liquidity-Sweep Scalper — Backtest & Audit

A backtest of the session-based "liquidity sweep → displacement → FVG
inversion" scalp for Nasdaq futures. Every chart concept from the spec is
turned into a numeric rule, so the strategy can be tested before any broker
connection is built.

**Status: the engine is validated, but the strategy has NOT been validated.**
No real 1-minute NQ history could be downloaded from the build environment
(Alpha Vantage intraday is a premium endpoint, and Yahoo was blocked). The
audit below proves the backtester is honest. It cannot say whether the
strategy makes money; only running it on real NQ bars (step 2 below) can
answer that.

## The strategy, boiled down (as implemented)

All times are US/Eastern. Everything is on 1-minute bars.

1. **Windows.** Trade only during London (02:00–05:00) and the NY open
   (09:30–11:00). The sweep, the signal and the entry must all happen inside
   the window.
2. **Liquidity map (built at window start, using only past data).** Levels:
   the Asian high/low (20:00–24:00), the prior-day high/low, the London
   high/low (NY window only), and overnight swing highs/lows (pivot = the
   extreme of ±5 bars, known only 5 bars later). Equal highs/lows are 2+
   pivots within 3 ticks. A level already traded through before the window
   opens is dropped. Swings confirmed during the window are added as they
   appear.
3. **Sweep (long example; shorts are the mirror image).** A bar trades at
   least 1 tick below a sell-side level, and a bar closes back above it within
   3 bars.
4. **HTF filter.** Longs only if the swept level is below the prior day's
   midpoint (discount). Shorts only if it is above (premium).
5. **Displacement + FVG inversion.** Within 15 bars of the reclaim, a bullish
   candle must close at least 1 tick above the top of a bearish 3-candle FVG
   (candle 3's high < candle 1's low). The gap must have formed in the leg
   into the sweep and still be unfilled. The candle's body must be at least
   1.75× the median body of the prior 20 bars. If a close lands back below the
   swept level first, the setup is cancelled.
6. **Entry.** *Conservative* (default): a limit order at the top of the
   inverted FVG (the retest), good for 10 bars. It is cancelled if T1 trades
   first. *Aggressive*: a market order at the next bar's open.
7. **Stop.** 1 tick beyond the sweep extreme. The trade is skipped if the stop
   is more than 20 pts away (London), more than 30 pts (NY), or less than
   3 pts. The stop is never widened.
8. **Size.** `contracts = floor($260 / (stop_pts × $/pt))`. That is 1% of a
   $26,000 account on MNQ at $2/pt, capped at 50 contracts.
9. **Targets.** T1 is the nearest untaken opposing liquidity: highs/lows,
   swings, equal highs/lows, the midnight open, the prior RTH close (gap
   edge), or a 1H FVG edge. If T1 is not at least 2R away, the trade is
   skipped. At T1, half the position is closed and the stop moves to
   break-even. The rest exits at T2 (the next pool) or on a 60-minute time
   stop.
10. **Kill switches.** Max 2 fills per window. A −$520 day (2R) locks out
    trading until the next session.

Fills are deliberately pessimistic:
- 1 tick of slippage on market entries and on stop and time exits.
- Limit orders fill only if price trades *through* them by 1 tick.
- If a single bar touches both the stop and the target, the stop is assumed
  to fill first.
- MNQ fees are $1.24 round trip; NQ fees are $4.50.

## Files

- `scalper_backtest.py` — the engine (state machine, CSV loader, metrics, CLI).
- `scalper_audit.py` — the validation battery. It runs on synthetic data with
  no arguments, or on real data with `--csv`.
- `requirements.txt` — `tzdata` only (needed on Windows for UTC timestamps).
  Otherwise the code uses only the standard library.

RESULTS_PLACEHOLDER
