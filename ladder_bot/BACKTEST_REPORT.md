# 1-1-1-2 Put Step-Down Ladder — Backtest Report (SPY, Oct 2007 – Oct 2026)

**Status: BACKTEST ONLY. No orders have been placed.** The paper bot stays in
dry-run until you set `LADDER_BACKTEST_APPROVED=yes` in `ladder_bot/.env`.

Reproduce: `python ladder_backtest.py` (base case), plus the flags listed under
"Sensitivity" for the other runs. Each run writes every trade to `results/trades.csv`,
every skipped Friday to `results/skipped_entries.csv` and the numbers below to
`results/summary.json`.

## Verdict on "100% win rate except one ES crash"

**Not supported.** Holding every ladder to expiration (the YouTuber's version) wins
about 98.6% of the time, but losing trades come from **three separate selloffs**:

| Losing episode (expiration) | Losing ladders | Total loss | Worst ladder | Worst, scaled to today's SPY price |
|---|---|---|---|---|
| Oct–Dec 2008 | 5 | −$3,619 | −$1,946 | −$11,640 |
| Dec 2018 | 3 | −$817 | −$407 | −$1,079 |
| Mar 2020 | 4 | −$14,889 | −$3,998 | −$9,584 |

The 2008 and 2020 losing episodes appear under every assumption I tested.
Depending on the skew assumption, Dec 2018 or Jun 2022 also produces losses.
The structure only loses when SPY settles more than about 20% below the entry
price, so any 3-month drop of roughly 15–20% hurts it. That happened in 2008
and 2020, and nearly in 2018 and 2022.

The bigger problem is the **payoff shape**:

- An average win is **+$80** and an average loss is **−$1,610**, so one loss
  erases about 20 wins.
- About 13 ladders are open at once (one per Friday, 90 DTE), so a crash hits
  all of them together. The mark-to-market drawdown was **−$46k**, roughly the
  same as the entire 19-year profit of **+$47k**.

## Base-case results (skew 0.30, $0.02/contract slippage, credit filter on)

| | Hold to expiry (baseline) | Stop: intraweek touch of BE | Stop: weekly close below BE |
|---|---|---|---|
| Ladders closed | 850 | 850 | 850 |
| Win rate | 98.6% | 96.6% | 97.5% |
| Average win | $80 | $62 | $70 |
| Average loss | −$1,610 | −$934 | −$2,121 |
| Largest single loss | −$3,998 | −$2,581 | −$4,395 |
| Largest loss at today's SPY size | −$11,640 | −$5,138 | −$10,199 |
| Total P&L, 19 years | +$47,378 | +$23,417 | +$13,588 |
| Max drawdown (weekly mark-to-market) | −$46,392 | −$16,512 | −$40,363 |
| Avg cash-secured BP per ladder (K4 × 100 − credit) | $25,883 | same | same |
| Avg Reg-T margin per ladder (for comparison) | $2,772 | same | same |
| **Annual return on cash-secured BP** | **0.86%** | 0.43% | 0.25% |
| Annual return on Reg-T margin | 8.1% | 4.0% | 2.3% |
| Peak cash-secured BP, all open ladders | $964,285 | same | same |

The stop variant reduces the drawdown, but it also cuts profit, because it
locks in losses on selloffs that recover. For example, the April 2025 selloff
cost −$8.6k under the touch stop and nothing under hold. Since the backtest
only has weekly bars, a daily stop would land somewhere between the two stop
columns.

### By year (ladders grouped by the year they expired/closed)

| Year | Hold: win rate / P&L / worst | Stop (touch): win rate / P&L / worst |
|---|---|---|
| 2008 | 86.1% / +$1,030 / −$1,946 | 77.8% / +$759 / −$687 |
| 2020 | 92.3% / −$5,225 / −$3,998 | 78.8% / −$12,909 / −$2,241 |
| 2022 | 100% / +$15,032 / +$26 | 100% / +$15,032 / +$26 |
| 2025 | 100% / +$12,144 / +$40 | 88.5% / −$3,678 / −$2,581 |

2008's dollar figures look small because SPY was trading at $70–$130. In
today's dollars, the worst 2008 ladder was −$11.6k.

## Trade math (per ladder, shown for every trade in trades.csv)

- Max profit at the bottom strike = (W1 − W2 + W3) × 100 + credit = 2w × 100 + credit
- Lower breakeven = K4 − max profit / 100
- Below K4 the ladder behaves like one naked short put: each $1 SPY falls loses $100

**Example using today's chain shape** (SPY $774, 15 Jan 2027 expiry, VIX ~16):

- **Strikes:** buy 725, sell 706, buy 687, sell 2× 668 (spacing $19)
- **Credit:** about $0.98
- **Max profit:** about $3,898
- **Breakeven:** about $629 (−18.7%)
- **SPY −20% → −$982**, −30% → −$8,722, **−40% → −$16,462**
- **Cash-secured requirement:** about $66,800 per ladder (668 × 100 − credit)

Across the backtest, the average spacing was 2.6% of spot. That matches the
original's 150 ES points (~3%). The average breakeven sat 20.6% below entry.

## Account checks

- **SPX:** Alpaca now lists index options (SPX, SPXW, XSP) for paper and live
  trading. SPX is 10× SPY, so one SPX ladder would need about $630k
  cash-secured, which isn't practical. **XSP** (mini-SPX, 1/10 of SPX) is
  SPY-sized, cash-settled and European-style, so it can't be assigned early.
  It is the closer match to ES. The bot uses **SPY** for now, because I can't
  test XSP quotes or orders without your keys.
- **Uncovered short put:** Alpaca does not allow uncovered short options. Its
  Level 3 rules say an mleg order is accepted *only if every leg is covered
  within the same order*. In this ladder, K1/K2 and K3/K4 form two covered put
  spreads, which leaves **one K4 short put uncovered**.
  - **The 1/1/1/2 mleg order you asked for may be rejected outright.** The bot
    submits it as one mleg (never separate legs), logs any rejection, and
    stops there.
  - **Tested on paper (Oct 9 2026): Alpaca accepted the 1/1/1/2 mleg for both
    SPY and XSP.** It holds the leftover short put cash-secured at its full
    strike, less the credit: K4 × 100 − credit. That's **about $68k per ladder
    today** (SPY 680 ladder: $67,968 held; XSP 685: $68,500).
- **20% buying-power cap:** the strategy reaches about 13 concurrent ladders.
  At today's prices that's about $800k+ of cash-secured BP, so a 20% cap
  needs an account of roughly **$4–5M** to run it as designed. On a $100k
  account the cap is $20k, which is **less than one ladder, so the bot would
  skip every Friday.** The cap is enforced on every entry.

## Return on capital, plainly

On cash-secured capital (Alpaca's treatment), the strategy earned under 1% a
year, less than T-bills, while carrying crash risk. It only reaches about 8%
a year on Reg-T naked-put margin, and the original reaches its numbers on
SPAN futures margin. Alpaca offers neither.

## How much to trust this

- **Real data:** SPY prices (Alpha Vantage weekly, unadjusted), settlement
  prices, crash timing and VIX (CBOE daily).
- **Modeled:** option premiums and deltas, via Black-Scholes with VIX-derived
  90-day IV plus a put-skew model.
  - Your Alpha Vantage key is free-tier. HISTORICAL_OPTIONS, index data and
    full daily history all came back as "premium endpoint", so real 2008–2023
    option chains weren't available.
  - I couldn't cross-check against Alpaca's 2024+ option bars either, because
    no Alpaca keys are available in this environment.
- **Credit sensitivity:** the opening credit is small ($0.30 average) and very
  sensitive to skew, so I ran sensitivity cases:

| Run | Ladders | Win rate | Total P&L | Max DD | Ann. return on cash-secured | Loss episodes |
|---|---|---|---|---|---|---|
| Base (skew 0.30) | 861 | 98.6% | +$47k | −$46k | 0.9% | 2008, 2018, 2020 |
| Flatter skew 0.20 (`--skew 0.20`) | 358 | 98.9% | +$8k | −$26k | 0.3% | 2008 ×2, 2020, 2022 |
| Steeper skew 0.40 (`--skew 0.40`) | 990 | 99.1% | +$104k | −$30k | 1.9% | 2008, 2020 |
| No credit filter (`--no-credit-filter`) | 990 | 87.1% | +$44k | −$46k | 0.8% | 2008, 2018, 2020 (+small debits) |
| Wider slippage $0.05 (`--slip 0.05 --slip-stressed 0.15`) | 564 | 98.6% | +$29k | −$43k | 0.6% | 2008, 2018, 2020 |

- **Weekly bars:** stops are only checked weekly (that's all the free data
  allows).
- **Assignment:** SPY options can be assigned early, and early assignment
  isn't modeled. XSP would remove that risk.
