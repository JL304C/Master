# NVDA Delayed Iron Condor — Backtest

Tests the "delayed iron condor" on NVDA: open a ~45 DTE put credit spread when NVDA
is declining but finding support, then add a same-expiry, same-width call credit
spread with 2–4 weeks left if NVDA has risen and no earnings are before expiry.

Run: `python3 nvda_delayed_condor_backtest.py --trades` (standard library only).

## "Declining but finding technical support": the testable definition

The phrase needed a precise definition before it could be tested. On weekly bars:

| Condition | Rule |
|---|---|
| Declining | The lowest price of the last 2 weeks is ≥7% below the 5-week closing high |
| Support | The nearest level below the close among the 10-week SMA (~50-day), 20-week SMA (~100-day), 40-week SMA (~200-day), or the prior swing low (lowest low of weeks −12…−3) |
| Near it | The close is no more than 6% above that support |
| Tested and held | The last 2 weeks' low came within ±3% of support without breaking it by more than 3% |
| Selling stalled | The week closed up, or closed in the upper half of its range |

- **Put strikes:** short put at support −2% (never above 0.30 delta), long put one width lower.
- **Expiry:** ~6 weeks. If an earnings report falls before that, the expiry is shortened to the last Friday before the report, provided at least 4 weeks remain; otherwise the signal is skipped.
- **Call add:** with 2–4 weeks left, if NVDA is at least 2% above the entry price, within 3% of its 10-week high, or at weekly RSI ≥ 65, and no earnings fall before expiry. Short call below 0.20 delta, long call one width higher.
- **Management:** if NVDA touches a short strike, that spread is closed; otherwise both spreads are held to expiry.

## Data and modelling limits

- Alpha Vantage's free tier blocks full daily history and historical option chains. The backtest therefore uses
  real split-adjusted **weekly** NVDA bars from 2012 to Sep 2026 and prices the options with
  **Black-Scholes**. Implied volatility is modelled as realized volatility × 1.10 plus a put skew. Real fills will differ.
- Every trade is rescaled to today's price ($224.58), so "$5" and "$20" wide mean what they mean now.
- **Costs:** Alpaca charges $0 commission; the model includes about $0.03 per contract in pass-through regulatory fees and a bid/ask
  cost of max($0.03, 2%) per leg on every open and close.
- Closing a spread at the touch of the short strike is optimistic when NVDA gaps through the strike.

## Results (1 contract, 14.7 years)

| Variant | $20 wide: trades / win / total P&L / avg / worst / max DD | $5 wide total |
|---|---|---|
| A. Put spread only (signal) | 21 / 62% / +$2,361 / +$112 / −$327 / −$574 | −$61 |
| **B. Delayed iron condor (as specified)** | **21 / 52% / +$2,742 / +$131 / −$327 / −$880** | +$124 |
| C. Calls on 50% of contracts (2 put, 1 call) | 21 / 67% / +$5,103 (2× size) / +$243 / −$655 / −$1,284 | +$64 |
| D. No signal: 5% OTM put spread every cycle | 98 / 64% / +$10,007 / +$102 / −$528 / −$1,579 | +$1,481 |
| E. No signal + delayed call add | 98 / 47% / +$8,120 / +$83 / −$1,416 / −$2,696 | +$319 |
| F. Delayed IC, held through earnings | 30 / 63% / +$6,069 / +$202 / −$343 / −$1,070 | +$809 |

Findings:
1. **Modestly profitable at $20 wide, not at $5 wide.** At $5 wide the credit is so small that
   bid/ask and fees eat it.
2. **The support signal is rare, about 1.4 trades a year.** Per trade it earns 6.8–7.8% on the buying power held,
   about the same as selling a put spread every cycle with no signal (6.2%). Because the
   signal skips most of the time, total dollars are much lower. The edge comes mostly from NVDA's long uptrend and the
   put premium, not from the timing.
3. **Adding the call spread is roughly break-even and noisy.** It added +$381 over 14 adds at $20 wide.
   Without the signal it lost −$1,882 over 66 adds, because NVDA tends to keep rising
   after it rises. It also lowers the win rate from 62% to 52%.
4. **The sample is too small to be sure.** Across 21 trades the average profit is +$131 with a standard deviation of $329
   (t ≈ 1.8, not statistically significant). The 2023–2026 period was net negative.
5. The result holds up under changes to the IV premium, skew, pullback size and near-support distance (every variant stays
   positive). The call-delta setting is the least stable parameter.
6. Holding through earnings looks better here, but the weekly model understates gap-through-strike losses. It is not
   recommended on this evidence.

**Current state (week of 2026-09-24):** there is no new signal. A signal on 2026-08-28 would still be open, with an Oct 9 expiry.
