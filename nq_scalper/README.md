# NQ/MNQ Liquidity-Sweep Scalper — Backtest & Audit

A backtest of the session-based "liquidity sweep → displacement → FVG
inversion" scalp for Nasdaq futures. Every chart concept from the spec is
turned into a numeric rule, so the strategy can be tested before any broker
connection is built.

**Verdict: FAILED on 10 years of real NQ data. Do not automate this as specified.**
It was tested on 3.5M one-minute bars (Databento, Sept 2016 – Sept 2026,
front month). It failed every one of the four pre-committed pass rules
(below).

## Real-data results (NQ, 2016-09-25 → 2026-09-23, net of costs, MNQ at $260 risk)

| | Result |
|---|---|
| Trades | 281 over 2,583 sessions (~28/yr, one every ~9 days) |
| Win rate | 28.8%. Avg win $535, avg loss −$283. Break-even needs ≈34.6%. |
| Expectancy | **−$47/trade (−0.21R)**, profit factor 0.77 |
| Net P&L | **−$13,287** (gross before fees and slippage: −$4,476) |
| Before *any* costs | −$1,584 (≈ −$6/trade). No raw edge at all. |
| Max drawdown | **$17,766 = 68% of a $26k account** |
| 95% bootstrap CI of expectancy | −$92.66 … +$0.74 |
| First half / second half | −$6,260 / −$7,028. Both lose. |
| London / NY | −$5,549 (211 trades) / −$7,739 (70 trades) |

**Pass rules** (committed before seeing the data):
1. Both halves net-positive: **FAIL** (both negative).
2. The CI must exclude zero: **FAIL**. It sits almost entirely below zero.
3. No ±20% change may flip the result to a loss: **moot**. All 16
   sensitivity and variant runs lost money, from −$3,177 (displacement 2.1×)
   to −$94,314 (first-pool-≥2R target).
4. Drawdown tolerable: **FAIL** (68% of the account).

Before costs, the mechanical rules performed like a coin flip: on real data
they matched the no-edge random-walk test. After fees and slippage (about
$31/trade, since tight stops mean more contracts), they lose steadily.
Choosing the least-bad variant would be curve-fitting to noise, because
every variant is negative.

Caveat: this tests *this* numeric reading of a discretionary method. A
human trader's judgment (context, news, order flow) is not captured. What
it does show is that the rules as written, automated, have no edge on
10 years of NQ.

### Re-test: major levels only (`--levels major`)

In the first run, 88% of trades (247/281) swept minor 5-bar swings rather
than the pools the strategy is built on. This re-test allows sweeps only of
the Asian/London/prior-day highs/lows and equal highs/lows. It also **fails**:

| | Result |
|---|---|
| Trades | **42 in 10 years** (~4/yr) |
| Expectancy | −$33/trade (−0.22R), PF 0.83, net **−$1,367** |
| Before any costs | +$476 total (≈ +$11/trade), wiped out by ~$34/trade costs |
| 95% CI | −$142 … +$93 (includes zero) |
| First / second half | −$1,131 / −$236. Both lose. |
| Max drawdown | $2,492 (9.6%) |
| Variants | 13 of 15 lose. London-only (+$379, 32 trades) and displacement 2.1× (+$178, 28 trades) are slightly positive, which is what noise produces when you test 15 variants on ~30 trades. |

Conclusion: whether the levels are read broadly or strictly, the rules as
written show no edge on 10 years of NQ that survives costs. Even if the
strict version had a small edge, 4 trades a year could not be told apart
from luck, or be worth automating.

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
   3 pts. These caps are set for NQ at 24,000 and scale with price (30 pts
   becomes about 19 pts at NQ 15,000), so older years are tested fairly. The
   stop is never widened.
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

## Audit results (synthetic data — this tests the engine, not the market)

Run with `python scalper_audit.py` (takes about 20 minutes on 4 cores). Each
path is one year (250 sessions) of 1-minute NQ-like bars: 22% annual vol,
fat tails, a quiet Asian session and a volatile NY open. Account size is
$26k, trading MNQ at 1% risk.

| Test | Result |
|---|---|
| 0. Lookahead check (truncate or scramble the future; the past must not change) | **PASS**: 77 trades identical across 9 cuts |
| 1. Null test: random walk with no edge, 40 years | Median **−$46/trade**, **−$1,800/yr**, Sharpe −0.65. The engine is not biased toward profits. |
| 2. Positive control: planted stop-hunt reversal, 40 years | Median **+$91/trade**, +$2,800/yr, Sharpe 1.04, 82% of years profitable. The engine does detect a real edge when one exists. |
| 3. Cost drag | $16/trade = **0.06R**, the minimum real edge needed just to break even |

What the synthetic runs already reveal about the strategy itself:

- **Luck can look like an edge.** With zero edge, **30% of one-year runs
  still made money** (the top 5% made +$4,600). A profitable year of
  backtesting or paper trading proves nothing on its own.
- **Signals are rare.** All the required conditions happen in sequence only
  about **30–50 times a year** (≈0.15 per day), not several scalps a day.
  Per-trade results are very noisy (σ ≈ 2R). Confirming a +0.2R edge at
  roughly 2 standard errors takes about **400 trades (≈10 years of data)**.
  A +0.3R edge takes about 180 trades (≈4–5 years). Months of data is not
  enough.
- **Limit-retest fills carry adverse selection.** Even with zero fees, the
  null test loses −0.11R per trade gross. A limit order only fills when price
  trades through it, so it tends to fill on the trades that keep going
  against you. This is real-world behavior, not a bug.
- **Fragile parameters.** A ±20% change in the displacement threshold moved
  Sharpe by −56% to +69%. `min_rr` moved it −23% to +36%. Other parameters
  moved it ≤12%. Anything that sensitive has to be re-checked on real data
  and must not be tuned to one sample.
- **"Nearest pool must be ≥2R" is what holds it together.** Loosening it to
  "first pool that is ≥2R" quadruples the trade count and turns the planted
  edge into a loss (−$34/trade).
- **The NY stop cap may be too tight.** With a 30 pt cap, NY produced only
  about 7 trades a year, because NY-open sweeps usually need wider stops.
  Calibrate the caps on real data.
- In this planted-edge model, aggressive entry beat the retest by a wide
  margin. That result belongs to how the edge was planted (it reverses
  immediately) and may not carry over to real data.

## Getting the real answer (required before any live trading)

1. **Get real 1-minute NQ bars, 3–5+ years.** On the Databento website
   (Batch download):
   - **Dataset:** CME Globex MDP 3.0.
   - **Product:** "NQ • E-mini Nasdaq-100 Futures". The website can't select
     the continuous `NQ.c.0` symbol (only the API can), and it doesn't need
     to: the loader keeps only the highest-volume contract each session (the
     front month) and drops the other expiries and spreads.
   - **Schema:** OHLCV-1m. Not Trades, which is about 100× larger.
   - **Encoding:** CSV. **Compression:** none. Turn on "map symbols" and the
     pretty timestamps/prices options if offered. The loader copes either
     way.

   Other OHLC sources (NinjaTrader, Sierra Chart, TradingView export,
   FirstRate Data) also work. The CSV needs a timestamp column plus
   open/high/low/close.
2. **Run it:**
   ```powershell
   pip install tzdata
   python scalper_audit.py --csv NQ_1min.csv --tz UTC        # Databento timestamps are UTC
   python scalper_backtest.py --csv NQ_1min.csv --tz UTC --trades-out trades.csv
   ```
   Use `--tz America/Chicago` for CME-local exports, or leave it off if the
   timestamps are already Eastern. The audit prints full-sample stats, a
   bootstrap 95% CI on expectancy, first-half vs second-half results, and the
   ±20% sensitivity and variant table. `trades.csv` lists every trade, so you
   can check them against a chart.
3. **Decision rule** (set now, before seeing the results):
   - Proceed to paper trading only if **both halves** are net-positive after
     costs.
   - The bootstrap CI must **exclude zero**.
   - No ±20% change may flip the result to a loss.
   - Max drawdown must be tolerable at your risk per trade.
   - Anything less means the strategy is unproven. Do not optimize parameters
     until it passes; that only fits noise.

## Not modeled

- **Economic news** (CPI, NFP, FOMC): the spec says to avoid scheduled
  high-impact news. There is no calendar here, so the backtest trades through
  it. Add a skip-date list before relying on the results.
- **Queue position** beyond the 1-tick trade-through rule, partial fills, and
  latency.
- **Data quality**: the price jump on roll days (the front month switches
  on the highest-volume day, with no back-adjustment) and bad ticks.
- **Some partial rules**: "break of a local swing" on the displacement candle,
  and the trailing stop on the runner (the runner uses T2, break-even, or a
  60-minute time stop instead).
