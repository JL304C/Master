# Contrarian Short-Selling Bot: Strategy Specification (v2)

**Purpose:** Build one intraday short-selling bot with four selectable strategies, backtest each one, then run on Alpaca paper trading.
**Primary source:** Mike Herman / Warrior Trading, *Advanced Technical Trading*, Chapters 1–5 (circa 2017), including the annotated chart examples in Chapters 2–4.
**Secondary source:** Warrior Trading *VWAP Trading Strategy* course, used only as optional add-ons (Section 9). It must not replace the contrarian logic.
**Owner:** Jeff Leach
**Data:** Databento (historical + live US equities). TradingView for visual spot-checks.
**Broker:** Alpaca (paper first).

> **Note to Claude Code:** The course teaches discretionary chart reading. This spec turns it into rules. Where the course was vague, a concrete choice was made and marked **[DEFAULT]**; all of these belong in `config.yaml` as starting values to test, not settled facts. Anything marked **[ADDED]** is not from the course; it was added for safety or realism. Quotes in "double quotes" are taken from the course slides or chart annotations.

---

## 0. The core idea (read this first)

The course's edge, stated in Chapter 1: "Locating price level zones of significance produces nonrandom reactions which allow for predictable inflection points." Most intraday price action is random; the bot should only act at the few moments where it is not.

Chapter 3 defines the contrarian approach as "selling of resistance, preferably after a sharp, violent move in price." When the crowd leans into a move that fails, "the reversion move is often explosive as the trend traders exit positions."

Nearly every chart example in Chapters 2–4 shows the same four-part sequence:

1. **A significant level on the daily chart.** This is a pivot zone, a trendline, or a gap edge.
2. **A sharp extension move into the level, intraday.** This could be an opening gap, a spike, or a rally. It ideally comes with a high-of-day volume bar or a volume ramp. The course says a volatile move into a level produces "a higher quality trade in relation to a slow controlled trend."
3. **Rejection.** Price spends no more than a few minutes beyond the level, then closes back inside it. A lower high follows.
4. **A fade.** Price falls back toward VWAP and then to the next structure level. The course's examples show moves of 5 to 13 points (KITE, STMP, VERI).

Strategies A, B and C are this same sequence applied to the three kinds of level the course teaches. Strategy D is the course's trend-continuation setup. All four share one **Rejection Engine** (Section 4) and one **Trade Manager** (Section 7).

---

## 1. Build order

1. Data layer: download and cache 1-minute and daily bars. Build 2-minute and 5-minute bars from the 1-minute bars. (Optional v2: Databento 1-second bars, aggregated to 15s and 30s for the entry timeframe the course actually uses.)
2. Daily level library (Section 3) with unit tests, plus a chart dump of the levels it finds for visual checks.
2b. Stock selection (Section 6): market tone, scanners, favorites, Daily Watchlist. Include premarket bars (04:00–09:30 ET) in the data layer for this.
3. Rejection Engine (Section 4) and strategies A–D.
4. Trade Manager (Section 7), Risk module (Section 8), event-driven backtester (Section 10).
5. Backtest report (Section 11). **Stop here and show results before building live trading.**
6. Alpaca paper-trading runner that uses the same strategy code (Section 12).

Backtest and live trading must call the **same** strategy, level, and risk functions. Only the data feed and the order router should differ.

---

## 2. Architecture

```
config.yaml
data/          # Databento loaders, bar aggregation (1m -> 2m, 5m), cache
selection/
    market_tone.py    # SPY/QQQ/IWM/DIA premarket + live tone (6.1)
    scanners.py       # gappers, reversal, relative weakness, level proximity (6.2)
    watchlist.py      # merges scanners + watchlist.yaml -> Daily Watchlist (6.4)
watchlist.yaml # Jeff's favorites list (6.3)
manual_levels.yaml # Jeff's own levels (3.5)
reports/prep/  # daily morning prep sheet (6.5)
levels/        # daily pivots, trendlines, gaps, MAs -> list of Level objects
indicators/    # vwap, ema, sma, atr, rvol, swing points, candle metrics
engine/
    rejection.py      # the shared contrarian state machine (Section 4)
strategies/
    a_pivot_fade.py
    b_trendline_fade.py
    c_gap_fill_fade.py
    d_downtrend_continuation.py
    addons/vwap_crack.py, addons/vwap_fade.py   # optional, off by default
trade/         # scaling in, stops (Scenario B -> C), structure targets, trailing
risk/          # sizing, limits, kill switch
execution/     # sim_broker.py, alpaca_broker.py
backtest/      # runner, metrics, report, trade chart images
live/
tests/
```

`Level` object: `{kind: pivot|trendline|gap_fill|gap_entry|sma|psych|premarket_high|prior_high|manual, zone_low, zone_high, strength, touches, first_seen, notes}`. For trendlines, the zone is the line's value for today ± a band.

Every signal, order, fill, stop move and exit is logged with the strategy letter and a reason string, for example `"A: 2nd test of pivot 57.10-57.45 failed, 1m lower high -> add"`.

---

## 3. Daily levels (Chapter 2): the foundation of everything

The course says "support & resistance are zones, NOT a finite number." Levels are rebuilt every morning before 09:30 from daily bars (lookback 500 days **[DEFAULT]**). Only levels **above** the current price (prior close, then refreshed live) matter for a short bot. Old support that price has fallen below becomes resistance; the role flips. The course's KITE, CLVS and ICPT examples are all broken support retested from below.

`ATRd` = 14-day daily ATR.

### 3.1 Pivot zones (Ch. 2 "Pivots")
The course describes pivots as "created over long periods of time where close consistency in price action is identifiable." In the examples (EBAY, CLVS, DLTR, KITE, STMP, SRPT), several daily highs, lows or closes line up at the same price.

- Candidate points: daily swing highs and swing lows (bar beyond the 3 bars on each side **[DEFAULT]**), plus daily closes at the ends of consolidation runs.
- Cluster any points that fall within `0.35 × ATRd` of each other **[DEFAULT]**.
- A valid pivot needs **≥ 3 touches spread over ≥ 10 trading days** **[DEFAULT]**.
- Zone = the range of the clustered prices, widened by `0.1 × ATRd` on each side.
- `strength` = touches + a bonus for span (months) + a bonus for touches that had high volume. The course says "multiple tests on a daily time frame further validates a level," and "extreme volume bars into pivot further validate level" (SRPT).
- A pivot is **invalidated** if price closes beyond it for more than 2 consecutive days ("on a daily time frame, no more than 2 trading days MAX outside"). After that, it flips role.

### 3.2 Trendlines (Ch. 2 "Trend Lines")
The course says trendlines should anchor on the **most extreme swings** (first, second and third order pivots), because "utilizing the most extreme moves in the market will remove the subjectivity."

- Find major daily swing highs and lows using a zig-zag that requires a reversal of ≥ 3 × ATRd **[DEFAULT]**. These are the first and second order pivots.
- **Standard (no-cut) line:** connect two major swing highs (falling resistance) or two major swing lows (rising support), with no daily close crossing the line between them. It becomes valid when a third touch lands within `0.25 × ATRd` of the line ("multiple extreme tests create valid trendline").
- **Resistance lines for shorts:**
  - a falling line through lower highs, with price approaching from below;
  - a rising support line that price has **broken** (closed below for 2+ days), now retested from below. The course's example is ICPT: "Retest of daily trend line provides entry for fade."
  - the upper rail of a parallel channel (copy the support line's slope onto the opposite extreme).
- Today's zone = line value for today ± `0.15 × ATRd`.
- Complex "best fit, cut-through" lines are **v2**. They are too subjective for v1.

### 3.3 Gaps (Ch. 2 "Gaps")
The course says gaps leave "a 'pocket' where there is a lack of support or resistance," and "either side of the gap will act as support or resistance."

- An unfilled daily **down-gap** of ≥ 2 × ATRd or ≥ 4% **[DEFAULT]** creates two levels above price:
  - **gap fill** = the pre-gap day's low (the top of the void);
  - **gap entry** = the post-gap day's high (the bottom of the void).
- When a stock rallies into the void, there is "no resistance until gap fill." That makes the gap fill edge the main short level. The course's example is KORS: "Surge into the gap fill zone on an exhaustion bar … stock remains weak for days after failed gap fill test."
- The gap is marked filled once a daily close is above the gap fill level. It is then removed.

### 3.4 Other structure (used as levels and targets)
- Daily SMA 50, 100 and 200, and the daily EMA20 (the course's indicator set), each as a zone of ± 0.1 × ATRd. Mike's prep sheet treats an SMA that **coincides with a pivot** as a key level ("317-318 (200SMA, pivot)"). When an SMA zone overlaps a pivot zone, merge them and add +2 strength.
- **Psychological round numbers** (from the prep sheet: TSLA "300 (psych level)"): whole multiples of $50 for stocks over $200, $10 for $50–200, $5 for under $50 **[DEFAULT]**. Zone ± 0.1 × ATRd. Low strength on their own; +1 strength when they coincide with another level.
- **Premarket high and low** (from the prep sheet: BABA key resistance = "pre mkt highs"): today's premarket high is an intraday resistance level from 09:30.
- Prior day high and low, and the high and low of the prior 52 weeks.

### 3.5 Manual levels
Mike drew his levels by hand each morning (6.5). `manual_levels.yaml` lets Jeff add levels for any symbol, which are treated like a pivot with strength 5:

```yaml
2026-10-19:
  TSLA: {resistance: [303.4, "317-318"], support: [300, 290.75]}
```

Manual levels are logged separately in the report, so Jeff can see whether his own levels beat the automatic ones.

### 3.6 Daily context filter for shorts
- Skip a level if today's open is more than 3 × ATRd above it, because the stock has run far through it already **[DEFAULT]**.
- Levels closer together than `0.5 × ATRd` merge into one zone that keeps the strongest type and the summed strength.

---

## 4. The Rejection Engine (Chapters 3–5): shared by A, B and C

A per-stock, per-level state machine runs on **1-minute bars** (the entry timeframe), confirmed on **2-minute** bars and managed on **5-minute** bars. This follows the course's multi-timeframe slide: "tick charts … helpful in initiation; once trade is initiated, switching to a 2 minute time frame for further confirmation will allow for an add; trade management … 5 minute time frame."

`ATR1` = 14-period ATR on 1-minute bars. `ATR5` = the same on 5-minute bars.

### State 1 — ARMED
The stock is on the Daily Watchlist (Section 6.4), the current market tone allows the strategy (Section 6.1), and price is within `1.0 × ATR5` below an active resistance level.

### State 1b — FOCUS (from the Primus interview, 2017-11-02)
Warren (Primus) on how the Contra window is meant to be used: an alert is not a trade. "We're starting to focus on it … watching every higher high on these 1-minute candles, until a point is reached where it stops going up." Also: "DLTR came up in the Quick Strike first and then it migrated over to the Contra."
- **Pre-focus:** a Quickstrike High print puts the stock on high alert; it is streamed and charted from that moment.
- **Focus starts** on the first Contra Short print (6.2b). From then on, track on 1-minute bars: the run of consecutive higher highs, peak Vdif, and the highest 1-minute volume of the run.
- **Focus ends** if 30 minutes pass without a valid rejection, or if Vdif falls back below its focus-start value without a trade **[DEFAULT]**.
- **Logged:** time from Quickstrike to Contra, Vdif at focus start, peak Vdif. The interview's examples grew 1.09→2.30 (W, afternoon), 0.98→1.50 (QRVO) and 1.29→2.92 (W, morning) before turning, so the report should show whether Vdif expansion predicts the fade.

### State 2 — EXTENSION (the "sharp, violent move")
At least one of these must be true while price enters the zone:
- **Speed:** price climbed ≥ `2.0 × ATR5` over the last 15 minutes or fewer **[DEFAULT]**;
- **Opening gap/spike:** the stock opened or spiked into the zone in the first 30 minutes (DLTR, CLVS: "pivot retest on open provides ideal entry for fade");
- **Wide-bodied candles:** ≥ 2 consecutive 1-minute bodies ≥ 1.5× the average 1-minute body of the session. The course says "extreme bodied candles often indicate exhaustion/blowoff point especially when multiple occur in a consecutive fashion."

If price drifted into the level slowly (none of the above), **do not trade the first touch.** Log it as `"slow approach"`. The course says a volatile move is "a higher quality trade in relation to a slow controlled trend as these are more likely to breach through the level and continue."

### State 3 — EXHAUSTION (optional boosters, logged and scored)
These raise the setup score but are not required. The course says volume "will not occur on every trade but does not exclude the trade."
- **Volume climax:** a high-of-day volume bar (on 1-minute or 5-minute) prints in the zone (CY, SRPT: "High of day volume bar into resistance pivot = EXHAUSTION").
- **Volume ramp:** at least 4 consecutive 1-minute bars of rising volume into the zone (MO, MU).
- **Body constriction:** after the wide bodies, the next 1–3 bodies shrink to ≤ 50% of the prior bar's body. The course says "As candle bodies tighten or constrict relative to prior bars an entry point often presents itself," ideally at the level.
- **Scanner confluence (Section 6.2):** Contra Short tier hit (+1 per tier), Quickstrike High printing in the same 5 minutes (+2), SP Top Reversal conditions (+1), `gap_reversal` flag (+1). The DLTR example on 2017-11-02 had Contra Short and Quickstrike High firing together at the top.

`setup_score` = level strength + boosters. Log it on every trade so the report can show which features actually matter.

### State 4 — REJECTION (aggressive entry)
- Price may poke above the top of the zone, but it must **close back below the zone top within 3 one-minute bars** **[DEFAULT]**. The course allows "no more than a few minutes at MAX spent outside the established support/resistance."
- If price closes beyond the zone for longer than that, the level has failed. Cancel the setup and do not short this level again today.
- The rejection bar must close in its lower half.
- **"Stops going up" trigger (interview):** for a stock in FOCUS, the rejection bar is the **first 1-minute bar that fails to make a higher high and closes red** (close < open) after a run of ≥ 3 consecutive higher highs **[DEFAULT]**. Preferred, not required: the highest-volume bar of the run printed in the last 3 bars, and the rejection bar's volume is lower ("at a certain point the volume peaks"). Warren: "You wait for a red candle and you put out an offer." This is the same idea as the course's rejection and body-constriction rules, made precise for the bot.
- **Entry 1 (starter, 1/3 of planned size):** sell-stop at rejection-bar low − $0.01, valid for 3 one-minute bars.
- **Initial stop, Scenario B:** spike high + `max(0.5 × ATR1, 0.1% of price)` **[DEFAULT]**. The course says to place it "several ticks … outside of the obvious extreme," scaled to "current market movement." Never place it exactly 1 tick above the high (Scenario A), because "the majority of stops will be placed in this zone … responsible for poor fills."

### State 5 — CONFIRMATION (the add)
The course says "Lower highs following resistance test confirms entry point" (ICPT, TSLA, NVDA).
- After Entry 1, a **1-minute lower high** forms below the spike high, **and** a 2-minute bar closes below the 2-minute EMA9 (timeframe alignment).
- **Entry 2 (add the remaining 2/3):** sell-stop at the low of the bar that made the lower high − $0.01.
- **Move the stop, Scenario C:** move the stop on the whole position to just *inside* the spike high, i.e. lower-high + `0.25 × ATR1`, but never higher than the original stop. The course says to use Scenario C "once level has confirmed as an inflection point," which avoids the cluster of obvious stops.
- **Risk cap:** after the add, total dollar risk must stay ≤ the planned risk per trade (Section 8). If the add would breach it, reduce the add size.
- **Disconnect rule:** if the 2-minute chart is not aligned with the 5-minute chart (2-minute still making higher highs), skip the add and keep only the starter (HMHY: "2 minute time frame not aligning with 5 minute").

`entry_mode` in config: `scaled` (default: starter plus add, as above), `confirmed_only` (no starter; full size at Entry 2), or `aggressive_only` (full size at Entry 1).

### State 6 — SECOND TEST (alternate path)
Many course examples enter on the **second** test rather than the first spike (RLYP: "Secondary test and fail which resulted in a strong move lower"; UBNT: "Two lower highs close below trendline provides entry for fade").
- If the first rejection did not trigger Entry 1, or the stock was stopped out, re-arm **once**. A second test that makes a lower high inside the zone and rejects can be traded with the same rules.
- **Do not take a third test.** The course says "Multiple tests on a trading time frame weakens the level … Be suspicious if after 2 or more tests there isn't a measurable inflection."

---

## 5. The four strategies

### Strategy A — Daily Pivot Rejection Fade (main contrarian setup)
**Level:** pivot zones (3.1), plus SMA/EMA zones and the 52-week high as lower-weight levels.
**Course examples:** RLYP, CLVS, DLTR, KITE, STMP, CY, SRPT (Ch. 2 and Ch. 4).
**Rules:** the Rejection Engine (Section 4) against the pivot zone.
**Extra filter:** pivot `strength` ≥ the configured minimum **[DEFAULT: 3 touches]**.
**Typical shapes:**
- the stock gaps or spikes up into an overhead pivot at the open and fails (CLVS, DLTR, STMP);
- the stock gapped *below* a pivot that used to be support, rallies back up into it, and gets rejected several times (KITE: "Multiple rejections at pivot provide short entry opportunity").

### Strategy B — Daily Trendline Rejection Fade
**Level:** resistance trendlines (3.2): a falling resistance line, a broken rising support line retested from below, or a channel upper rail.
**Course examples:** ICPT ("Retest of daily trend line provides entry for fade"), UBNT, TSLA ("Multiple failed tests of trend line; lower highs after failed test provides entry opportunity"), and the NVDA intraday descending line.
**Rules:** the Rejection Engine against today's trendline zone.
**Difference from A:** the State 2 extension requirement is relaxed to `1.5 × ATR5` **[DEFAULT]**, and `entry_mode` defaults to `confirmed_only`. The course's trendline examples nearly all enter on lower highs *after* the failed test, not on the spike.
**Also from the course:** "Quick, violent spikes beyond the short-term trend line is often a buying … climax … a natural exhaustion point." A spike that overshoots the line and closes back below within the time limit counts as a valid rejection.

### Strategy C — Gap-Fill Rejection Fade
**Level:** the gap fill edge of unfilled daily down-gaps (3.3).
**Course example:** KORS: "Surge into the gap fill zone on an exhaustion bar … stock remains weak for days after failed gap fill test and fades lower."
**Rules:** the Rejection Engine against the gap fill zone (zone = gap fill price ± 0.15 × ATRd).
**Extra condition:** the approach is a rally *through the void*, from gap entry toward gap fill. The pocket has no resistance until gap fill, which is what creates the extension.
**Targets:** the first structure target is the gap entry edge (bottom of the void) if it is closer than VWAP. The course says the pocket allows "a clean and controlled move."

### Strategy D — Downtrend Continuation (Ch. 3 "Trend Continuation")
The one strategy here that is *not* contrarian. The course pairs it with the contrarian approach, and it lets the bot keep earning after a fade turns into a trend.
**Course example:** VERI: "News catalyst creates initial move … Downtrend continuation entry/add/scale points against 20EMA … 10 point move."
**Setup**
1. **Initial move**, either of:
   - a sharp drop of ≥ 3 × ATR5 within the session, on above-average volume (RVOL ≥ 2): a gap down, a Gap Down and Go Down print, or an intraday break; **or**
   - a **Trendsell** trigger **[DOCUMENTED scan]**: on the daily chart SMA10 < SMA20 and SMA10 falling, at least 1 day of higher highs, then price breaks below yesterday's low today. The documentation says "when the alert occurs it becomes a daytrade."
2. **Established downtrend on 5-minute:** at least 2 lower highs, price below the EMA20, and EMA20 sloping down.
3. **Pullback:** a 5-minute bar's high reaches the EMA20 zone (± 0.15 × ATR5) and the bar closes below the EMA20.
**Entry:** sell-stop at the pullback bar low − $0.01. Each later valid pullback may **add**, up to 3 entries in total, with the stop moved down so total risk stays capped.
**Initial stop:** pullback high + 0.25 × ATR5.
**Exit, trend broken:** the course says "remain vigilant as to when the trend breaks and becomes conclusively wrong." Exit everything on a 5-minute close above the EMA20 **and** a break of the last lower high.
**Patience:** no fixed first target. The course says "Important to remain extra patient with trend continuation due to the normally slow, controlled movements." Partial exits happen at structure levels only (Section 7).

---

## 6. Stock selection: market tone, scanners and watchlist

The course chose stocks in a set order: read the overall market premarket, run scanners, check a favorites watchlist, then look for setups only in the names that came through. The bot does the same, in four steps. Steps 6.1–6.3 run premarket and produce the **Daily Watchlist** (6.4). Only stocks on the Daily Watchlist are streamed and handed to the strategies.

> **Sources for the scanner rules:**
> 1. **"Warrior Scan Layout (August 2017)"**, Warrior's own documentation of its scan windows. It gives exact formulas for most scans. Rules taken from it are marked **[DOCUMENTED]**.
> 2. Screenshots Jeff took from a course recording (Warrior Scan Workstation, built on **Primus Scan**, 2017-11-02): an Edit Filter dialog with a complete rule, the Filter Manager list of saved filters, the full scanner layout, live scan output for W and DLTR, and Mike's morning prep sheet for that day.
> 3. A transcript of the Primus demo with Mike Herman and Warren (the Primus scanning specialist), recorded the same day. It explains how the windows were used live (quoted in State 1b, State 4, 6.2b and 6.4).
> 4. Anything not covered by these sources is marked **[INFERRED]**.
>
> From the transcript: Mike's own basket was called **"MH1"**; its contents are unknown. The demo used a broader basket ("100,000 average daily volume, 5 to 200"). The favorites watchlist (6.3) stands in for MH1.
>
> The VWAP course mentions "reversal scans" that find "stocks with high volume trading at extremes." The Contra scans are those reversal scans.

### 6.0 Base universe
- **Course basket (from the screenshot):** "AvgVol 100k 5 to 200", i.e. average daily volume ≥ 100,000 shares and price $5–$200. Always excluded: volatility products (the screenshot lists DWT, XIV, UVXY, TVIX, SVXY).
- **This bot's default, a tighter version of the course basket for large caps:** US common stocks (no ETFs or ETNs; ADRs optional), price $15–$1,000, 20-day average dollar volume ≥ $50M **[DEFAULT]**. `universe_mode: course_basket | large_cap | sp500` in config switches between the course basket, this default, and the S&P 500 only. In backtests, use point-in-time membership if available; otherwise flag the survivorship bias.
- **Live, short-specific [ADDED]:** the Alpaca asset must be `shortable` and `easy_to_borrow`. Check at 08:30 and again before every order.

### 6.1 Premarket market tone (runs 08:00–09:25 ET, final reading at 09:25)
This step judges whether the overall market is strong or weak before the open, as the course did. Index ETFs stand in for the indexes, because they trade premarket on the same equities data feed:

| Proxy | Index | Weight **[DEFAULT]** |
|---|---|---|
| SPY | S&P 500 | 1.0 |
| QQQ | Nasdaq 100 | 1.0 |
| IWM | Russell 2000 (small caps) | 0.5 |
| DIA | Dow 30 | 0.5 |

Optional: SMH (semiconductors) as a fifth proxy, since semis often lead the Nasdaq. If index futures data becomes available later (ES, NQ), use it instead of premarket ETF prices, because futures trade overnight with more volume. Index proxies are configurable in `config.yaml`.

For each proxy, score:
- **Gap:** premarket last price vs prior close, in units of its 14-day daily ATR. Above +0.3 ATR = strong (+1), below −0.3 ATR = weak (−1), otherwise 0.
- **Location:** above the prior day's high = +1, below the prior day's low = −1, otherwise 0.
- **Premarket trend:** premarket last price vs premarket VWAP since 08:00. Above = +0.5, below = −0.5.

`market_tone` = weighted average score across proxies, then labelled:
- **WEAK** (≤ −0.75), **SOFT** (−0.75 to −0.25), **NEUTRAL** (−0.25 to +0.25), **FIRM** (+0.25 to +0.75), **STRONG** (≥ +0.75) **[DEFAULT thresholds]**.
- **Divergence flag:** if QQQ and IWM score with opposite signs, the market is mixed. Treat it as NEUTRAL and log it.

**Intraday update:** tone is recomputed every 5 minutes from each proxy's price vs its own session VWAP, so the bot notices if the market turns after the open. The premarket tone and the live tone are both logged on every trade.

**How tone affects each strategy [DEFAULT; every cell is configurable and must be tested]:**

| Tone | A, B, C (contrarian fades) | D (downtrend continuation) | Size |
|---|---|---|---|
| WEAK / SOFT | allowed | allowed | 100% |
| NEUTRAL | allowed | allowed only if the stock is weaker than SPY (6.2c) | 100% |
| FIRM | allowed, level strength must be in the top 30% | off | 50% |
| STRONG | allowed only on a **volume climax** at a top-10% level | off | 50% |

Reasoning: a contrarian short against a strong market fights two forces, so it needs the best levels and half size. Trend-continuation shorts only make sense when the market is not lifting everything. The backtest report must show results by tone (Section 11) so these rules can be kept or changed based on evidence.

### 6.2 Scanners (premarket at 09:00 and 09:25, then every minute during the session)

**a) Gappers, premarket and at the open [DOCUMENTED].**
- **Pre Market Bid Up 3.5%:** `bid / yesterday_close > 1.035` and premarket volume > 10,000 shares. Starts populating at 07:30. Use last trade price if bid data is not available.
- **Pre Market Ask Down 3.5%:** `bid / yesterday_close < 0.965` and premarket volume > 10,000.
- **Pre Market New Day High / Low:** a stock on either list above making a new premarket high (low). The documentation notes that a stock from the Down 3.5% list making premarket new highs "has reversed."
- **Gap Up Static (at 09:30):** `open / yesterday_close ≥ 1.01` **and** `open > yesterday_high`. Columns: `GAP = open − prior close`, `ChgOpen = last − open`, `NetChg = last − prior close`. Positive ChgOpen = following through upward; negative = "it has reversed down, even if NetChg is still positive."
- **Gap Down Static (at 09:30):** `open / yesterday_close ≤ 0.99` **and** `open < yesterday_low`.

**For this bot:** gap-ups from these lists are the "in play" candidates for opening spikes into levels (CLVS, DLTR, STMP-style setups). A gap-up whose **ChgOpen turns negative** is flagged as `gap_reversal`, a contrarian booster. Gap-downs are kept as candidates for Strategy D and for rallies back up into a broken pivot (KITE style).

**b) Contra Short scan (the course's main contrarian scan; exact rule known).** The Primus filter "NEW DAY HIGH Vdif 1.0" reads:

```
NewDayHigh(1, NO) AND ( Last(ALL_VENUES, CURRENT, NO) - VWAP(CURRENT, NO, VWAP, V1, PRIMARY) ) > 1.00
```

In words: the stock **just made a new high of the day**, and its last price is **more than $1.00 above session VWAP**. `Vdif` = Last − VWAP, in dollars **[DOCUMENTED]**.

What the documentation says about the official Warrior Contra scans: Contra Short lists "stocks which have moved up and meet the parameters of a proprietary formula." Its mirror, Contra Long, "is linked to 5 min candlestick chart. It continues to print as it makes new 5 minute lows." So the official Contra Short most likely **prints on each new 5-minute high** while the stock is stretched above VWAP **[INFERRED mirror]**. The exact formula was not published. Jeff's saved filter above is the course trader's own working version of it. The Filter Manager shows several saved variants ("NEW DAY HIGH Vdif 1.0", "New Day High Vdif 1.00", "NDH TEST", "1s test short 1"), which suggests the threshold was being tuned.

The layout shows three tabs, "Contra Short 1 / 2 / 3". These are most likely tiers with larger Vdif thresholds **[INFERRED]**.

**Live examples from 2017-11-02 (screenshots):**
- **W** (≈ $60): Vdif went from 1.29 at 10:02:24 to 2.95 at 10:04:56. The stock spiked from ≈ $57 to ≈ $62 and was rejected just under **62.6**, the key resistance pivot on Mike's prep sheet that morning (6.5). That is the complete method: a prepared level, the Contra scan catching the spike, then rejection.
- **DLTR** (≈ $94): Vdif rose from 1.78 to 2.68 between 12:19 and 12:22 while price spiked from ≈ $91.4 to $94.81 on a volume surge. **Quickstrike Hi printed at the same time** (mSMA20Dif 2.42, VN3m 7.63). The stock then faded.
- QRVO (≈ $74, Vdif 1.28–1.50): also in the list.

These are mid and large caps, not small caps. Typical Vdif at the top of the move was $2–3, about 3% of price.

Implementation, run on every completed 1-minute bar after 09:31:
- `new_day_high`: the bar's high exceeds every earlier regular-session high today. Option `contra_trigger: new_day_high | new_5m_high` (new_5m_high = the official scan's inferred behavior). The backtest compares both.
- `vdif = last − session_vwap`.
- **Course-faithful mode:** `vdif > 1.00` (Tier 1). Tiers 2 and 3 at `vdif > 2.00` and `> 3.00` **[INFERRED]**.
- **Normalized mode (default for this bot) [ADDED]:** a fixed $1 means very different things on a $20 stock and a $600 stock. So the default also requires `vdif ≥ 1.5 × ATR5` **and** `vdif / last ≥ 1.0%` **[DEFAULT]**. Tiers are 1.5×, 2.5× and 3.5× ATR5. `contra_mode: dollar | normalized | both` in config. The backtest compares them.
- Also require RVOL so far today ≥ 1.5 **[DEFAULT]**. The course's "in play" definition calls for high relative volume.
- A hit is logged with its tier. By default it is a Strategy A/B/C candidate only if the stock is within 1 × ATR5 of an active daily level (6.2d). The Contra scan finds the "sharp, violent move"; the level tells the bot where it should fail.
- **Level requirement is a test, not a certainty.** In the interview Warren says "anything that shows up in a contra window is actionable," and he traded QRVO and DLTR without mentioning any daily level. W, on the other hand, failed right at the prep sheet's 62.6 pivot. So `contra_requires_level: true | false`. When false, Contra hits go straight to FOCUS (State 1b) and are traded on the "stops going up" trigger, logged as **A0 – Contra exhaustion (no level)**. The report compares A0 with A/B/C, which shows whether Mike's levels add an edge on top of the scanner.
- **Scans are for generating ideas.** Mike, in the interview: "You can't go in and build a scan that you expect every hit on the scan to be tradable … you don't want to narrow it so far down that you are missing out on trades." The scanners stay broad. The filtering happens in the Rejection Engine, which is where the trader's decision is made.

**Contra Long** (the mirror image: new day low, Vdif < −1.00) is visible in the layout too. It is not used, because this bot is short-only, but it should be implemented alongside so a long side can be added later.

**c) Relative weakness.** The stock's % change today minus SPY's % change (or its sector ETF's, if mapped) ≤ −2% **[DEFAULT]**, with RVOL ≥ 1.5. These are Strategy D candidates: a stock weak on a day the market isn't.

**d) Level proximity (from Section 3).** Every stock in the base universe whose price is within 2 × ATRd below an active resistance level, ranked by level strength. This scan alone does not make a stock tradable today. It must also show up in (a), (b), (c) or the favorites list.

**e) The other scans in the course's scanner layout.**

Column definitions **[DOCUMENTED]**: `Vdif = last − VWAP`; `mSMA20Dif = last − SMA20 on the 1-minute chart`; `VN3m = 3-minute volume / average 3-minute volume`; `GAP = open − prior close`; `ChgOpen = last − open`. `VN` (Relative Volume Leaders) is not defined; it is treated as relative volume for the day so far **[INFERRED]**.

| Scan | Rule | Source | Used by |
|---|---|---|---|
| **Gap Up and Go Up** | Gap Up as defined in 6.2a; prints when price exceeds the **high of the first 5-minute bar**, and keeps printing on new day highs | DOCUMENTED | Extension input to the Rejection Engine (State 2): a gap that keeps running is what gets faded at a level |
| **Gap Down and Go Down** | Gap Down as in 6.2a; prints when price breaks the **low of the first 5-minute bar**, then on new day lows | DOCUMENTED | Strategy D candidates |
| **Quickstrike High** | Price **above the upper Bollinger Band (20, 2) on both the 1-minute and 5-minute charts**, **and** `last − SMA20(1-min) > $0.50`, **and** 5-minute volume ≥ 1.5 × average | DOCUMENTED | Exhaustion booster (State 3). DLTR on 2017-11-02 fired Quickstrike Hi and Contra Short together at the top. **Contra + Quickstrike confluence = the strongest booster.** Normalized version **[ADDED]**: `mSMA20Dif ≥ 1.0 × ATR1` instead of a fixed $0.50 |
| **Quickstrike Low** | Mirror: below the lower band, etc. | DOCUMENTED | Long side (not used); optional Strategy D context |
| **Trendsell** | Daily chart: **SMA10 < SMA20 and SMA10 falling**; at least 1 day of higher highs (an upward consolidation); then price **breaks below yesterday's low** and it becomes a day trade | DOCUMENTED | Strategy D trigger (see Section 5, D) |
| **Trendbuy** | Mirror for longs | DOCUMENTED | Not used (short-only) |
| **SP Top Reversal** | Saved filter description: "RSI>90, VN5M>2, mDstEMA20>…" (the rest is cut off): RSI above 90, 5-minute relative volume above 2, stretched far from the EMA20 | PARTIAL | Exhaustion booster: 1-minute RSI(14) > 90 **and** 5-minute RVOL > 2 **and** distance from 1-minute EMA20 ≥ 1.5 × ATR1 **[threshold INFERRED]** |
| **Consecutive Candles 5 Up** | 5 consecutive green candles (5-minute assumed) | INFERRED | Extension input (State 2) |
| **Para Up** | Parabolic run: ≥ 4 consecutive higher 1-minute closes with expanding range, total ≥ 3 × ATR1 | INFERRED | Exhaustion booster (State 3) |
| **Relative Volume Leaders** | Ranked by RVOL so far today | INFERRED | Watchlist ranking (6.4) |
| **MOMO New Day Highs** | New day high on the "Ross C Basket" (small-cap momentum list) | PARTIAL | Not used by default (small-cap momentum is a different course) |
| **VWAP Short / VWAP Cross Up** | Price at VWAP (Vdif near 0) | INFERRED | VWAP Crack / Fade add-ons (Section 9) |
| **Static Earnings** | Company reports today / after yesterday's close | INFERRED | Catalyst flag on the watchlist |

Other saved filters seen in the Filter Manager but not used in v1: SMA 50 Bounce (up/down, 5-minute), Truerangemover, Bigup, Massive Volume Low Float, News FDA, Gappers Under $20. They are mostly small-cap or long-side scans.

**Earnings/catalyst flag:** the course's "in play" definition includes "fueled by a catalyst." Stocks with earnings today get +1 in the watchlist ranking (6.4) and the flag is logged. An earnings calendar source is needed (Alpha Vantage's EARNINGS_CALENDAR or similar) **[ADDED]**.

### 6.3 Favorites watchlist (Jeff maintains it)
A plain file, `watchlist.yaml`, lists stocks that are always monitored, whatever the scanners say. They still must pass the borrow check, the level rules, and every strategy rule. Being on the list only guarantees the bot is watching them.

Seed list (course-chart stocks that still trade, plus liquid large caps that behave the same way). **Jeff edits this list:**

```yaml
favorites:
  # from the course's chart examples
  - NVDA
  - TSLA
  - AAPL
  - NFLX
  - MU
  - MO
  - DLTR
  - SRPT
  - STX
  - URI
  - AAL
  - QCOM
  - EBAY
  - CSX
  - BIDU
  - ISRG
  - AAP
  # liquid large caps often "in play"
  - AMD
  - META
  - AMZN
  - GOOGL
  - MSFT
  - AVGO
  - SMCI
  - COIN
  - BA
notes: "Edit freely. Add or remove symbols; the bot reloads this file each morning."
```

Note: the course examples are from 2015–2017. Several of those companies were acquired or renamed (KITE, CY, RLYP, UBNT, ICPT), so they are left out.

### 6.4 The Daily Watchlist (built at 09:25, updated intraday)
1. Start with the stocks from **(a) gappers** + **favorites** that also have an active resistance level within 2 × ATRd above (from 6.2d).
2. Rank by: level strength × 2 + gap size (in ATRd) + premarket RVOL. Keep the **top 20** **[DEFAULT]**.
2b. **Morning short bias ("focus stocks").** From the interview: Mike named W, TSLA and BABA before the open. W was "coming down hard into the open," bounced from 56.50 to 61, and then "the contra short code kicked in and told you to go short again." Mark the top 3–5 stocks as **focus stocks** with a short bias if they meet any of: on the Pre Market Ask Down 3.5% list, a Gap Down Static, a Trendsell setup on the daily chart, or a key resistance within 1 × ATRd. Focus stocks get +3 in the ranking, are listed first on the prep sheet (6.5), and can be traded by Strategy D at the open and by A/B/C when they bounce into resistance later. This is the W pattern. Warren logged in at about 8:30 for this step; the bot runs it from 08:30 to 09:25.
3. During the session, add any stock that hits **(b) Contra Short** while within 1 × ATR5 of an active level, or **(c) relative weakness** / Gap Down and Go Down (for Strategy D). Maximum 40 symbols streamed at once; drop the lowest-ranked symbols that have not moved in 60 minutes.
4. Log the full watchlist each day with the reason every symbol was added (`gapper`, `favorite`, `contra_t1/t2/t3`, `rel_weak`, `earnings`), so the report can show which source produces the best trades.

### 6.5 Morning prep sheet (output every day at 09:25)
Mike's course published a premarket prep sheet each morning. Jeff's screenshot of the 2017-11-02 sheet:

| Stock | Key Support | Key Resistance | Additional Notes |
|---|---|---|---|
| W | 60 (200SMA, pivot), 56.75 (gap entry) | 62.6 (pivot) | Watch for break below 200SMA for fade |
| TSLA | 300 (psych level), 290.75 (pivot) | 303.4 (pivot), 317-318 (200SMA, pivot) | Watch for relief move and continued fade |
| BABA | 190 (trendline), 189 (trendline), 177.93 (20EMA) | pre mkt highs | Watch for break below trendlines for fade back to 20EMA / long above pre mkt highs if holds above trendlines |

The bot writes the **same table** for every stock on the Daily Watchlist, to `reports/prep/YYYY-MM-DD.md` and the log:
- **Key Resistance:** the 2 nearest active levels above price, each with its type(s) in parentheses, e.g. `62.6 (pivot)` or `317-318 (200SMA, pivot)`.
- **Key Support:** the 2–3 nearest levels below (these become the targets in Section 7).
- **Additional Notes:** a generated line naming the setups armed for that stock, e.g. "Armed: A (pivot 62.6) if spike on volume; D if break below 60 (200SMA)", plus market tone and the watchlist reason.

This gives Jeff a one-glance morning summary in the course's own format, and a simple way to check the levels against his charts. The W example shows why it matters: the 62.6 pivot on the sheet was exactly where the spike failed.

**Backtest note:** the backtest must rebuild the Daily Watchlist each day using only data available at that time (premarket bars up to 09:25, then intraday bars as they complete). No future data may influence which stocks are selected.

---

## 7. Trade Manager (Chapter 5)

### 7.1 Targets: the market structure approach
The course says to "look toward the next major level and set limit orders for portions of your position," and to "always place your limit orders several ticks inside the level."
- After entry, build an ordered list of support levels *below* the entry: **session VWAP**, the intraday low of day, prior intraday pivots, daily pivot zones, daily SMA/EMA zones, gap entry edges, and the prior day low.
- **T1, T2, T3** = the first three support levels whose distance from entry is ≥ 1.0R, 2.0R and 3.0R respectively. Each limit order goes `max($0.03, 0.10 × ATR5)` **inside** the level, i.e. above support for a short.
- **Exit in thirds:** cover 1/3 at T1, 1/3 at T2, and trail the last 1/3. The course says to exit "in 25% - 33% portions."
- **Hybrid minimum [DEFAULT]:** if no support level lies at least 1.5R below entry, use 2R as T1. This is the course's "risk multiple approach."
- After T1 fills: move the stop on the remainder to breakeven.
- **Runner:** trail the stop to the most recent 5-minute lower high + 0.25 × ATR5. Exit on the stop or at the end-of-day flatten.

`target_mode` in config: `structure` (default), `risk_multiple` (2R/3R/4R ladder), `hybrid`.

### 7.2 Stops: summary
| Phase | Stop location | Course name |
|---|---|---|
| Starter entry | Spike high + max(0.5 × ATR1, 0.1%) | Scenario B |
| After confirmation add | Lower high + 0.25 × ATR1 (inside the extreme) | Scenario C |
| After T1 | Breakeven | — |
| Runner | Latest 5-minute lower high + buffer | trail |

Every position always has a resting buy-stop at the broker. Stop orders are placed `$0.01–0.02` away from round numbers **[ADDED]**, because of the course's liquidity warning.

### 7.3 Scaling in
See Section 4, State 5. The course says "you are not near a full size position until the move has begun to CONFIRM and only then are you able to double or triple your position while adjusting your stop."

---

## 8. Risk management [mostly ADDED; the course leaves sizing to the trader]

The course warns that contrarian trading "is often responsible for dramatic trading losses to those who are unable to properly manage a position." These limits are hard-coded and cannot be overridden by strategies.

| Rule | Default |
|---|---|
| Planned risk per trade (full position, entry to stop) | 0.5% of equity |
| Max notional per position | 20% of equity |
| Max open positions | 3 |
| Max attempts per stock per level per day | 2 (first test + second test, Section 4) |
| Daily loss limit | −1.5% of equity, then no new entries today |
| Weekly loss limit | −4% of equity, then pause and alert |
| Never add to a losing position beyond the planned risk | enforced in `risk/` |
| End of day | flatten everything by 15:55 ET; no overnight shorts |
| Kill switch | a config flag and a command that cancel all orders and flatten |

Shares = floor(planned_risk_$ / (stop − entry)), capped by max notional. The starter = 1/3 of that number.

**Session timing:** contrarian entries are allowed from 09:31 (opening spikes into levels are among the course's best examples). No new entries after 15:30. A lunch filter (12:00–13:30) is **off by default** for A–C, because the course's NFLX example triggers at 12:20, and **on** for the VWAP add-ons, as that course specifies.

---

## 9. Optional add-ons from the VWAP course (disabled by default)

Keep these as separate modules, `addons.enabled: []`. They are not part of the core four. When enabled, they use the same Trade Manager and Risk module.

- **E — VWAP Crack.** The stock is in play and "put in a lower high and/or rejected a key resistance level; preferably on the daily chart." A 5-minute bar closes below VWAP. Price retests VWAP and closes back below with an up-thrust, pin bar or spinning top. Short below the confirmation candle low. Stop: above the confirmation candle high, or a 5-minute close back above VWAP. Reduce on the first 5-minute close above EMA9.
- **F — VWAP Fade.** The stock is in play and has moved up off the lows into VWAP. It fails to close above VWAP after at least one attempt. Short on the confirmed rejection. Stop: above the highest VWAP retest. T1 = low of day. Reduce on a 5-minute close above EMA9.

**Integration with the core:** the course's VWAP Crack examples (URI, UNFI, STX) all happen *under daily overhead resistance*. So E can be restricted to stocks that A, B or C rejected earlier the same day (`vwap_crack_requires_level_rejection: true`). That makes it a continuation entry after a contrarian rejection, not a standalone strategy.

---

## 10. Backtest requirements

**Data:** Databento 1-minute OHLCV bars (regular trading hours plus premarket for gap context) and daily bars. Period: from the earliest date your Databento US equities dataset covers (likely 2018) to the present.

**Note on the course examples:** almost all chart examples are from 2015–2017, which likely predates Databento's equities history. Several of those tickers (KITE, CY, UBNT/UI, RLYP) were acquired, delisted or renamed. Use them as **visual references** for what each setup should look like, not as replay tests.

**Fill model (conservative):**
- Sell-stop entries fill at the trigger price, or at the next bar's open if price gaps through, plus 2 bps slippage.
- Buy-stop exits fill at the stop price, or at the bar's open if price gaps through, plus 3 bps slippage. Add 5 bps more if the stop is within 1 tick of the obvious extreme (Scenario A), to model the course's liquidity warning.
- Limit exits fill only if price trades *through* the limit by at least $0.01.
- If a stop and a target are both inside one 1-minute bar, assume the stop filled first.
- $0 commission; $0 borrow fee (intraday, easy-to-borrow only).
- No lookahead: daily levels use only data up to the prior close; intraday logic uses only completed bars.

**Validation:**
- In-sample = first ~60% of the period, out-of-sample = the most recent ~40%. The out-of-sample period must not be touched while tuning.
- Tune a few parameters at a time. Report the number of combinations tried.
- Show a parameter sensitivity table for the 5 most important defaults: extension threshold, minutes allowed outside the zone, pivot touch minimum, Scenario B buffer, T1 minimum R.

## 11. Backtest report

For each strategy separately, all four combined, and each add-on if enabled:
- trades, win rate, average R, expectancy (R per trade), profit factor, net P&L, CAGR, max drawdown, Sharpe, longest losing streak, equity curve;
- **A0 (Contra exhaustion with no level) vs A/B/C (with a level)**, focus stocks vs other stocks, Quickstrike→Contra sequence vs Contra alone, and results by peak Vdif bucket;
- breakdowns by **premarket market tone and live tone** (WEAK → STRONG), **watchlist source** (gapper, favorite, contra tier 1/2/3, rel_weak, earnings), **Contra mode** (dollar vs normalized Vdif), level type and strength, first test vs second test, `entry_mode`, `target_mode`, time of day (09:31–10:00, 10:00–11:30, 11:30–13:30, 13:30–15:30), setup score buckets, and with or without each booster (volume climax, volume ramp, body constriction);
- for A–C: how often the add (Entry 2) triggered, and the result of starter-only trades vs full-size trades;
- the 10 best and 10 worst trades, as 1-minute and 5-minute chart images with the level zone, entry, stops, targets, VWAP and EMAs drawn. These are for manual review in TradingView against the course's chart examples;
- a daily chart image for 20 random levels found by `levels/`, so Jeff can confirm they look like the course's pivots, trendlines and gaps. **This check comes before trusting any results.**

---

## 12. Live paper trading (Alpaca)

- **Paper endpoints only.** A `live: false` flag must be flipped explicitly, plus a confirmation prompt, before any real-money endpoint can be used.
- **08:00 ET:** start streaming premarket bars for the index proxies and the base universe; compute market tone continuously (6.1).
- **08:30 ET:** refresh borrow status, rebuild daily levels, reload `watchlist.yaml`.
- **09:00 and 09:25 ET:** run the premarket scanners and build the Daily Watchlist (6.4). Write a short premarket summary to the log: market tone, divergence flag, watchlist with reasons.
- **During the session:** stream 1-minute bars (Databento live), aggregate to 2-minute and 5-minute bars, and run the same engine as the backtest.
- **Orders:** sell-stop entries; a resting buy-stop on every position; the bot manages partial exits itself, without bracket orders for multiple targets. Reconcile positions with Alpaca every minute.
- **Logging and alerts:** a decision log with reason strings, plus a daily summary. Alert on errors, rejected orders, and the kill switch.
- Real-money day trading in a margin account under $25k is subject to the pattern day trader rule. This does not apply in paper.

---

## 13. Known limitations and open questions

- **Selected examples.** The course's charts were picked to illustrate winning trades. Real win rates will be lower.
- **Discretion.** The course relies on chart reading (which pivots matter, where a trendline goes). The level library in Section 3 is an approximation. The level-chart check in Section 11 is how to catch gaps between the two.
- **Entry timeframe.** The course enters on 15-second and 30-second charts. v1 uses 1-minute bars; v2 can use Databento 1-second bars for finer entries.
- **No long side.** The course teaches buying support too, with the same logic in mirror image (MO, NFLX, CXW, MU examples). This bot is short-only. A `side: short|long|both` flag in the engine would make adding it later straightforward.
- **Different market.** Markets in 2026 behave differently from 2016–2017. The backtest decides; a negative result is a valid and useful outcome.
