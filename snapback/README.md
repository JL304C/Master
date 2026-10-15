# Snapback Entry backtest (Variant A: Asian-session box)

Backtest of the "Snapback Entry" opening-box strategy from the spec, Phase 1 only.
The confirmed rules are the defaults in `config.yaml`. Everything else in the spec
(touch depth, turn candle, re-entry, stop size, breakeven, trailing modes, targets,
box size filter, Tokyo box time, immediate-breakout entry) is a parameter that the
report sweeps.

## Layout
```
config.yaml           all parameters, the sensitivity list and the in-sample grid
src/data.py           Databento download (cost check first, cached per symbol/year), resampling
src/box.py            box candle, break / snapback / invalidation state machine (1-hour bars)
src/backtest.py       trade simulation on 5-minute bars, costs, 1% risk sizing
src/report.py         stats, tables, charts
src/run.py            driver: baselines, 70/30 split, grid pick, walk-forward, sensitivity, verdict
tests/test_rules.py   hand-built bar sequences for every rule
pine/                 TradingView strategy() version for the visual cross-check
```

## How it reads the rules
- Box = high/low (wicks) of the 1-hour candle starting 18:00 ET; DST handled by time zone.
  The Tokyo comparison is `box_open_time: "09:00"`, `box_tz: Asia/Tokyo` (20:00 ET summer, 19:00 ET winter).
- Trading day runs to the next box candle; one trade per day; flat at the day's end (`eod_exit`).
- Break = first 1-hour close beyond the box. Snapback = a later 1-hour candle (within `max_wait_bars`)
  whose wick re-enters the box, closes outside, and (toggle) closes in the trade direction. Entry at its close + slippage.
- A close back inside before the snapback cancels that side (unless `allow_reentry_after_close_inside`);
  a close beyond the other side cancels and lets the opposite setup arm. A setup with no snapback
  inside `max_wait_bars` expires.
- Stops and targets are checked on 5-minute bars. If one 5-minute bar touches both, the stop wins.
  A bar that opens through the stop fills at its open. Breakeven and trailing move only at 1-hour closes,
  and only in the trade's favour.
- Swing trail: when an hourly candle makes a new extreme after at least one pullback candle,
  the stop moves to the pullback's extreme (± `trail_buffer` × H).
- Step trail: one step behind the highest hourly close, counted from the breakeven trigger.
- Days where the continuous contract rolls are skipped, so no trade straddles a roll.
- R multiples are after commission and slippage. Sizing risks 1% of equity on entry-to-stop distance.

## Run
```bash
pip install -r requirements.txt
python -m pytest tests                 # rule tests
python -m src.run --estimate           # Databento price for what isn't cached yet
python -m src.run --download           # only after you accept the price
python -m src.run                      # writes results/summary.md, trades_*.csv, charts
python -m src.run --csv NQ=file.csv    # any 1- or 5-minute OHLC CSV instead of Databento
```
The Databento key is read from `DATABENTO_API_KEY`, or supplied by a network secret on hist.databento.com.

## Outputs (`results/`)
`summary.md` (verdict, IS/OOS stats, win rate vs break-even win rate, baselines, grid pick,
walk-forward, sensitivity, per-year and per-weekday tables, pass criteria),
`trades_<inst>.csv`, `days_<inst>.csv`, `sensitivity_<inst>.csv`, `equity_<inst>.png`, `heatmap_<inst>.png`.
