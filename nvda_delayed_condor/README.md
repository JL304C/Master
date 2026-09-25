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

## Daily-bar rerun (Databento, Nov 2018 – Sep 2026, 7.9 years)

`nvda_daily_backtest.py` reruns the strategy on real **daily** NVDA bars (Databento XNAS.ITCH, from
`fetch_databento_daily.py`), simulating what the bot actually does day by day:

- the entry signal is checked on the last trading day of each week, using the same `condor_rules` code as the bot;
- strike touches are checked every day;
- the call spread can be added on any day with 14–28 days left.

Options are still Black-Scholes priced, with the same costs as before.
Run: `python3 nvda_daily_backtest.py` (needs `nvda_daily_databento.csv` next to it).

| Variant | $20 wide: trades / win / total / avg / worst / max DD / return per trade on BP / t | $5 wide total |
|---|---|---|
| A. Put spread only (signal) | 11 / 64% / +$1,818 / +$165 / −$365 / −$433 / 10.3% / 1.6 | +$177 |
| **B. Delayed iron condor** | **11 / 64% / +$1,946 / +$177 / −$365 / −$809 / 11.1% / 1.5** | +$79 |
| C. Calls on 50% (2 put / 1 call) | 11 / 73% / +$3,764 / +$342 / −$731 / −$996 / 10.7% / 1.6 | +$256 |
| D. No signal, put spread each cycle | 62 / 61% / +$7,346 / +$118 / −$382 / −$1,710 / 7.4% / 2.7 | −$273 |
| E. No signal + delayed calls | 62 / 52% / +$9,002 / +$145 / −$382 / −$1,771 / 9.1% / 2.7 | −$476 |
| F. Delayed IC through earnings | 14 / 57% / +$1,885 / +$135 / −$358 / −$957 / 8.4% / 1.2 | −$105 |

Over the same period, the weekly-bar backtest gave B +$1,218 across 12 trades, so daily bars look somewhat better.

What the daily data confirms or changes:
- **$20 wide works; $5 wide does not.** At $5 wide the credit barely exceeds the bid/ask spread and fees.
- **The signal picks better trades but rarely fires.** It earned 10–11% per trade versus 7.4% with no signal, but fired
  only 11 times in about 8 years. Trading every cycle made more money in total (+$7,346) because the capital is in use far more often.
- **The call add is a small, noisy positive in this period.** It added +$128 over 7 adds with the signal and +$1,659 over 44
  without it. Over the full 2012–2026 weekly test it was negative without the signal. Treat it as optional, not an edge.
- **Holding through earnings is worse, so keep avoiding it.**
- **Still not statistically proven.** t ≈ 1.5–1.6 for the signal strategies.
- **Data caveat:** Databento's daily bars include pre-market and after-hours trading, which can only make the backtest
  close spreads early more often than the bot would. Three days are flagged "degraded" by Databento.
- The CSV is **not committed**: Databento's licence restricts redistribution, so it stays on your laptop.

**Current state (week of 2026-09-24):** there is no new signal. A signal on 2026-08-28 would still be open, with an Oct 9 expiry.

---

# Paper-trading bot (`nvda_condor_bot.py`)

> **Current setting: `ENTRY_MODE = "every_cycle"`, `ADD_CALL_SIDE = False`** (chosen for the most trades).
> That is daily-backtest variant D: a $20-wide put credit spread every cycle, with the short put about 7% below the price
> (reference = price × 0.95, then −2%, delta ≤ 0.30) and no call side.
> Result per contract, Nov 2018 – Sep 2026: 62 trades (~8 a year), 61% won, +$7,346 total, worst trade −$382,
> worst drawdown −$1,710. By year: 2019 +$1,755, 2020 +$1,080, 2021 +$688, **2022 −$364**, 2023 +$1,451,
> 2024 +$890, 2025 +$1,731, 2026 (to Sep) +$478.
> With the bot's 2 contracts, double those numbers: about $3,200 of buying power held per trade and a −$3,420 worst drawdown.
> The settings described below (support signal, delayed call side) still work if you switch those two lines back to
> `"support_signal"` and `True`.


The bot trades the backtested rules on your **Alpaca paper** account (`paper=True` is hard-coded). The signal
logic lives in `condor_rules.py`, which both the bot and the backtest import, so they trade exactly the same rules.

**Settings** (at the top of the script):

| Setting | Value |
|---|---|
| `WIDTH` | $20 |
| `PUT_CONTRACTS` | 2 |
| `CALL_FRACTION` | 0.5 (1 call spread per 2 put spreads, backtest variant C) |
| `ACCOUNT_CAP` | $26,000 (maximum loss) |
| Expiry | ~45 DTE, 28 DTE minimum |
| Short put | support −2%, delta ≤ 0.30 |
| Short call | delta < 0.20, added with 14–28 DTE left |
| Minimum put credit | 10% of the width |

**What it does each run** (once a trading day, around 3:45 PM ET):
1. If there's an open NVDA option order, it waits, so it never places a second order on top of one that hasn't filled.
2. If a spread is open and NVDA touched its short strike today, it closes that spread with a two-leg market order.
   On expiration day it also closes any spread whose short strike is within $1 of the price, to avoid assignment.
3. If only the put spread is open, with 14–28 DTE left, NVDA has risen, is near resistance or is overbought,
   and no earnings fall before expiry, it adds the call spread. It adds the call side at most once per position.
4. If no position is open, on the week's last trading day, it checks the entry signal and opens the put spread.
   The order is a two-leg limit order at the mid-price credit.
   After a spread is closed early, the bot waits for its original expiration before looking for a new entry, as the backtest did.

**Earnings safety:** you must keep `nvda_upcoming_earnings.txt` up to date. The bot refuses to open a position unless that
file lists an earnings date *after* the expiry it would trade. The two dates in it now are **estimates**; replace them once NVIDIA
announces the real ones.

## Setup (Windows, same as the GPC bot)
1. Run `pip install alpaca-py`.
2. Copy this whole folder to your computer as its **own folder**, e.g. `C:\Users\jeffl\nvda_delayed_condor\`. Don't put it in
   the NVDA bull call spread bot's folder: each bot reads the `.env` and writes its logs next to itself. The bot needs
   `condor_rules.py` and both earnings files beside it.
3. Create a `.env` file there with your **paper** keys (see `.env.example`). Keys from a **second Alpaca paper account** are
   recommended. The bull call spread bot also trades NVDA options and treats *every* NVDA option position on its account as
   its own, so it could misread or close this bot's legs. (This bot is protected the other way round: it only touches
   contracts recorded in its own log, so **don't delete `nvda_condor_log.jsonl`**.)
4. Make sure your paper account is approved for **options level 3** (spreads). Check in Alpaca's paper dashboard.
5. For the first test run, use `python nvda_condor_bot.py --dry-run`. It makes every decision and logs it to
   `nvda_condor_log.jsonl` / `nvda_condor_trades.csv`, but submits nothing. To check today's entry signal on a day other
   than the last trading day of the week, add `--force-signal-check`.
6. Once the dry runs look right, schedule `python nvda_condor_bot.py` in Task Scheduler for weekdays at 3:45 PM ET, with
   "start in" set to the folder.

## Verified vs. not yet verified
- `python test_bot_offline.py` runs every decision path against simulated Alpaca responses built from real NVDA weekly bars:
  entry, mid-week wait, pending-order wait, stale earnings file, call add, no double add, touch-close, dry run, and the one-cycle wait.
  It also checks that alpaca-py accepts every multi-leg order. All tests pass.
- **Not yet run against the real Alpaca API.** One behaviour needs checking on the first real paper fill: the bot sends a two-leg credit as a
  **negative** limit price. That matches my understanding of Alpaca's convention, but I couldn't confirm it from the docs from here. If the
  convention were the reverse, Alpaca should reject the order rather than fill it at a bad price. Check that the first order fills as a credit.
- The bot uses Alpaca's free IEX price feed; its weekly highs and lows can differ slightly from the full-market (SIP) data.
