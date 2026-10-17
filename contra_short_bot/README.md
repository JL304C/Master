# Contrarian Short-Selling Bot

An intraday short-selling bot built from `STRATEGY_SPEC.md` (Mike Herman / Warrior Trading, *Advanced Technical Trading*, Ch. 1–5). It will be backtested on Databento data first, then run on Alpaca paper account #3 ($1M).

## Status
| Step (spec §1) | State |
|---|---|
| 1. Data layer: Databento 1-minute bars, daily and 2/5-minute aggregation, split adjustment | done |
| 2. Daily level library (pivots, major swings, trendlines, gaps, moving averages, round numbers, prior/52-week highs, manual levels), unit tests, level charts | done, reviewed against TradingView |
| 2b. Stock selection: market tone, scanners, Daily Watchlist, prep sheet | done |
| 3. Rejection Engine, strategies A, B, C, D and A0, replay tool with setup charts | done |
| 4. Trade Manager, risk limits, simulated broker, event-driven backtester | done |
| 5. Backtest report | not started |
| 6. Alpaca paper runner | not started |

## Data
| Use | Dataset | From | Why |
|---|---|---|---|
| Intraday 1-minute bars, premarket included, and daily bars for levels | `XNAS.ITCH` | 2018-05-01 | Nasdaq-exchange trades: about 9-21% of each stock's total volume, depending on the stock. Prices, VWAP and volume *ratios* (RVOL, volume spikes) track the full tape |
| Official daily volume | `EQUS.SUMMARY` | 2024-07-01 | Only used to measure each stock's Nasdaq share, so share-count thresholds (e.g. "premarket volume > 10,000") can be scaled |

`EQUS.MINI` was tried first and dropped. Despite its name it holds only ~3% of volume and almost no premarket.

The universe is the favorites list (`watchlist.yaml`), plus SPY, QQQ, IWM, DIA and SMH. Live trading will use Alpaca's SIP feed, which has full volume.

- **Splits:** Databento bars are not split-adjusted. `splits.yaml` lists the splits. On each day, levels and intraday warm-up bars use only splits whose ex-date has already passed. `python -m tools.check_splits` flags price jumps that look like a split missing from the list.
- **Ticker changes** (META was FB until 2022-06-09) are mapped in `config.yaml`.

## Stock selection (spec 6)
- `selection/market_tone.py`: premarket tone (gap, location and premarket trend of SPY/QQQ/IWM/DIA, weighted), live tone (price vs session VWAP), the QQQ/IWM divergence rule, and the tone → strategy policy table.
- `selection/scanners.py`: Pre Market Bid Up / Ask Down 3.5%, new premarket highs and lows, Gap Up / Down Static, Contra Short and Contra Long (dollar or ATR-normalized Vdif tiers), Quickstrike High and Low, SP Top Reversal, Para Up, 5 green candles, Gap Up and Go Up / Gap Down and Go Down, gap reversal, Trendsell, and relative weakness vs SPY. Each row uses only bars completed by the end of that minute.
- `selection/watchlist.py`: the 09:25 Daily Watchlist with rank, reasons and focus stocks, plus the prep sheet in Mike's format. Run `python -m selection.watchlist --day 2024-03-08 --hits` to also list that session's scanner prints, including which Contra Short prints were at a level.

## Strategies (spec 4–5)
- `engine/rejection.py`: the Rejection Engine shared by A, B, C and A0. One `Setup` follows one level through the states: armed, extension (speed, opening spike or wide candles), exhaustion boosters scored into `setup_score`, then rejection. A rejection is either a close back inside the zone in the lower half of the bar, or, in FOCUS, the first red bar without a higher high. Then come the Scenario B starter, the add on a 1m lower high plus a 2m close below its EMA9 (with the Scenario C stop and the risk cap), the disconnect rule, the second test and the level-failure rule. FOCUS (state 1b) tracks Contra Short runs, Vdif and Quickstrike→Contra timing.
- `strategies/fades.py`: which levels belong to A (pivots, major swings, manual levels, moving averages, 52-week high; strength ≥ 3), B (trendlines; entry_mode `confirmed_only`, extension 1.5 × ATR5) and C (gap fill).
- `strategies/d_downtrend_continuation.py`: D, which shorts pullbacks into the falling 5-minute EMA20 after a ≥ 3 × ATR5 drop on RVOL ≥ 2 (or a Trendsell trigger). It makes up to 3 entries under one risk budget and exits when the trend breaks.
- `engine/trader.py`: one `SymbolTrader` per stock lets only one setup hold an order or position at a time. It adds stocks to the watchlist intraday, and `DayGate` applies the entry window and the live market-tone policy.
- `engine/replay.py`: replays chosen days through the backtest's day loop and writes the decision log, the trades and 1-minute setup charts.

## Trade management, risk and backtest (spec 7, 8, 10)
- `trade/manager.py`:
  - **Targets:** T1, T2 and T3 are the first support levels at ≥ 1R, 2R and 3R below the entry. Supports are session VWAP, the low of day, today's 5-minute swing lows, daily support zones and the prior day's low.
  - **Limits** sit max($0.03, 0.10 × ATR5) above the support.
  - **Hybrid minimum:** if no support lies ≥ 1.5R below the entry, T1 is set at 2R.
  - **Exits:** cover 1/3 at T1 and move the stop to breakeven, cover 1/3 at T2, then trail the last 1/3 at the latest 5-minute lower high + 0.25 × ATR5.
  - Strategy D only exits at structure levels.
- `risk/limits.py`: max 3 open positions (including working entries), the −1.5% daily loss limit (realized + open), and the kill switch. Sizing (`risk/sizing.py`) risks 0.5% of equity per full position, capped at 20% notional.
- `execution/sim_broker.py`: the spec's conservative fill model:
  - Sell-stop entries fill at the trigger (or a gap-through open), less 2 bps.
  - Stop exits fill at the stop (or a gap-through open), plus 3 bps, plus 5 more for a Scenario A stop.
  - Limits fill only when price trades $0.01 through them.
  - Stop before target when both are in one bar, and stopped out on the entry bar if that bar reached the new stop.
- `backtest/runner.py`: the day loop (bar fills → strategies → Trade Manager) and a parallel multi-year driver:
  - Sizing uses a fixed $1M, so days are independent; the −4% weekly limit is applied afterwards in date order.
  - The first 60% of days are in-sample and the rest out-of-sample.
  - Output goes to `reports/backtest/<name>/`: `trades.csv` (one row per position, with level, test, trigger, boosters, score, tones, watchlist reason, focus data, targets hit, R, MFE/MAE), the log, a config snapshot and `run.json`.

## Commands
```
pip install -r requirements.txt
python -m data.fetch --end 2026-10-16        # download / top up the cache (DATABENTO_API_KEY on the laptop)
python -m pytest -q tests
python -m tools.check_splits
python -m tools.level_charts --n 20          # charts + levels table -> reports/levels/
python -m tools.level_charts --symbol NVDA --day 2025-08-20
python -m selection.watchlist --day 2024-03-08 --hits   # prep sheet -> reports/prep/
python -m engine.replay --start 2024-03-04 --end 2024-03-15 --charts   # -> reports/replay/
python -m backtest.runner --start 2018-06-01 --end 2026-10-15 --workers 4 --name base   # ~2-3 h
```

## Choices made beyond the spec
Each one is marked in `config.yaml`.

- **Price-scaled pivot clustering.** Pivot clustering uses today's ATR as a **percentage** of price, not dollars. Over 500 days a stock can trade at very different prices.
- **Zone width cap.** A merged zone may be at most 1 × ATRd wide. Without the cap, nearby levels chained into bands 5 ATR wide.
- **Broken-support expiry.** A broken rising-support trendline only counts as a retest level for 60 sessions after the break. Older lines get projected far above price.
- **Pivot touches must be separated by a real move.** A new touch only counts after price has moved at least 1.5 × ATRd away from the level. Only the strongest pivot within 1.5 × ATRd is kept. Without these rules, choppy ranges were tiled with overlapping pivots.
- **Major swing levels.** Big zigzag highs and lows (≥ 3 × ATRd reversals) count as levels even with one or two touches, and two of them at the same price form a double top or bottom. Added after Jeff's chart review: the 3-touch pivot rule missed DLTR's 2022 double top, AAP's April 2023 high and BA's August 2023 top.
- **Centred pivot clustering.** Each swing point is tried as the centre of a zone that takes the points within 0.35 × ATRd of it. The old method swept up from the lowest price and split AAL's 8 swings at 13.78–14.16 into separate groups.
- **One trendline per type.** Only the best line of each type is kept (falling resistance, broken rising support, channel rail). Otherwise the same swings produced a fan of near-duplicate lines.
- **Entry engine refinements**, found by checking replay charts:
  - A test needs price approaching from **below**. A level that price has held above for more than 3 bars has flipped to support.
  - A rejection needs the spike to reach at least the zone's midpoint.
  - FOCUS ends only when Vdif falls 0.5 × ATR5 below its start, which stops it switching on and off every minute.
  - The add order lives 5 bars, and `confirmed_only` gives the lower high 20 bars to appear.
- **A-level filter.** Prior-day highs/lows and round numbers add strength but are not A levels on their own. Merged zones are capped at 0.6 × ATRd (was 1.0), and double tops pair swings within 0.25 × ATRd.
- **Backtest simplifications:**
  - Sizing equity is fixed at $1M (no compounding), so days can run in parallel.
  - The weekly loss limit is applied after the run.
  - Target mode `hybrid` is the same as `structure`, because the hybrid 2R minimum is always on.
  - T3 is recorded, but the last third trails rather than exiting at T3.
- **Strength defaults.** These are starting values: gap fill 3, gap entry 2, 200SMA 2, 52-week high 2, other moving averages and prior-day levels 1. Pivot strength = touches + 1 per 3 months of span (max 4) + 1 per high-volume touch (max 2).
