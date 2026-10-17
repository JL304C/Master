# Contrarian Short-Selling Bot

An intraday short-selling bot built from `STRATEGY_SPEC.md` (Mike Herman / Warrior Trading, *Advanced Technical Trading*, Ch. 1–5). It will be backtested on Databento data first, then run on Alpaca paper account #3 ($1M).

## Status
| Step (spec §1) | State |
|---|---|
| 1. Data layer: Databento 1-minute bars, daily and 2/5-minute aggregation, split adjustment | done |
| 2. Daily level library (pivots, trendlines, gaps, moving averages, round numbers, prior/52-week highs, manual levels), unit tests, level charts | done, **waiting for Jeff's visual check** |
| 2b. Stock selection (market tone, scanners, watchlist, prep sheet) | not started |
| 3. Rejection Engine and strategies A–D | not started |
| 4. Trade Manager, risk, backtester | not started |
| 5. Backtest report | not started |
| 6. Alpaca paper runner | not started |

## Data
| Use | Dataset | From | Why |
|---|---|---|---|
| Intraday bars (signals, VWAP, volume) | `EQUS.MINI` 1-minute | 2023-03-28 | Consolidated tape, so volume and VWAP are the real totals |
| Daily bars (levels) | `XNAS.ITCH` 1-minute, regular session only | 2018-05-01 | Long history for the 500-day lookback. Nasdaq-exchange trades only, so prices are fine but volume is a fraction of the total |

The universe is the favorites list (`watchlist.yaml`), plus SPY, QQQ, IWM, DIA and SMH.

- **Splits:** Databento bars are not split-adjusted. `splits.yaml` lists the splits. On each day, levels are built using only splits whose ex-date has already passed. `python -m tools.check_splits` flags price jumps that look like a split missing from the list.
- **Ticker changes** (META was FB until 2022-06-09) are mapped in `config.yaml`.

## Commands
```
pip install -r requirements.txt
python -m data.fetch --end 2026-10-16        # download / top up the cache (DATABENTO_API_KEY on the laptop)
python -m pytest -q tests
python -m tools.check_splits
python -m tools.level_charts --n 20          # charts + levels table -> reports/levels/
python -m tools.level_charts --symbol NVDA --day 2025-08-20
```

## Choices made beyond the spec
Each one is marked in `config.yaml`.

- **Price-scaled pivot clustering.** Pivot clustering uses today's ATR as a **percentage** of price, not dollars. Over 500 days a stock can trade at very different prices.
- **Zone width cap.** A merged zone may be at most 1 × ATRd wide. Without the cap, nearby levels chained into bands 5 ATR wide.
- **Broken-support expiry.** A broken rising-support trendline only counts as a retest level for 60 sessions after the break. Older lines get projected far above price.
- **Strength defaults.** These are starting values: gap fill 3, gap entry 2, 200SMA 2, 52-week high 2, other moving averages and prior-day levels 1. Pivot strength = touches + 1 per 3 months of span (max 4) + 1 per high-volume touch (max 2).
