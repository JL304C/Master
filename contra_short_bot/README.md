# Contrarian Short-Selling Bot

An intraday short-selling bot built from `STRATEGY_SPEC.md` (Mike Herman / Warrior Trading, *Advanced Technical Trading*, Ch. 1–5). It will be backtested on Databento data first, then run on Alpaca paper account #3 ($1M).

## Status
| Step (spec §1) | State |
|---|---|
| 1. Data layer: Databento 1-minute bars, daily and 2/5-minute aggregation, split adjustment | done |
| 2. Daily level library (pivots, major swings, trendlines, gaps, moving averages, round numbers, prior/52-week highs, manual levels), unit tests, level charts | done, reviewed against TradingView |
| 2b. Stock selection: market tone, scanners, Daily Watchlist, prep sheet | done |
| 3. Rejection Engine and strategies A–D | not started |
| 4. Trade Manager, risk, backtester | not started |
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

## Commands
```
pip install -r requirements.txt
python -m data.fetch --end 2026-10-16        # download / top up the cache (DATABENTO_API_KEY on the laptop)
python -m pytest -q tests
python -m tools.check_splits
python -m tools.level_charts --n 20          # charts + levels table -> reports/levels/
python -m tools.level_charts --symbol NVDA --day 2025-08-20
python -m selection.watchlist --day 2024-03-08 --hits   # prep sheet -> reports/prep/
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
- **Strength defaults.** These are starting values: gap fill 3, gap entry 2, 200SMA 2, 52-week high 2, other moving averages and prior-day levels 1. Pivot strength = touches + 1 per 3 months of span (max 4) + 1 per high-volume touch (max 2).
