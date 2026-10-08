# Broken-wing put condor backtester (SPX / ES / MES / XSP)

Backtests the "~90 DTE broken-wing put condor, opened every Friday" strategy
and runs the same structure on SPX, ES, MES (new cash-settled Micro options)
and XSP (Mini-SPX), so you can see whether it still works at the smaller sizes.

Each Friday it opens 4 puts with the same expiration:

| Leg | Action | Strike |
|---|---|---|
| L1 | buy  | ~20-delta put (`debit_delta`) |
| L2 | sell | L1 - 25 SPX points (`debit_width`) |
| L3 | sell | ~10-delta put (`credit_delta`) |
| L4 | buy  | L3 - 100 SPX points (`credit_width`) |

It only enters for a net credit (`min_credit`), holds to expiration by default,
and marks every open position to market daily. Weekly entries mean about 13
positions are open at once.

No live or paper orders. This is the backtest phase from the spec.

## Products

All strikes, widths, credits and `min_credit` are in **SPX-equivalent points**
for every product, so results line up. Only `slippage_per_leg` is in the
product's own quote units, because that is what the bid/ask looks like on screen.

| | SPX | ES | MES | XSP |
|---|---|---|---|---|
| $ per SPX point | $100 | $50 | $5 | $10 |
| Strike grid (SPX pts) | 5 | 5 | 5 | 10 ($1 XSP strikes) |
| Settlement | cash; 3rd-Friday AM, weeklies PM | quarterlies: cash (SOQ); serial months: into futures | cash, 3:00pm CT ES fixing | cash, PM |
| Style | European | American | European | European |
| Expirations modeled | Fridays to 120d, monthlies to 400d | monthlies to 400d, Fridays to 35d | **Fridays to 56d** | Fridays to 120d, monthlies to 400d |
| Margin model | Reg-T (max loss) | SPAN-like | SPAN-like | Reg-T (max loss) |

Things that matter for the MES/XSP question:

- **MES can't do 90 DTE today.** The new cash-settled MES options list about
  8 Fridays (~56 days) out, so `configs/mes.yaml` uses `target_dte: 49`. A
  90 DTE MES run skips every week with "no expiration within 90+/-7 DTE". If
  CME adds longer dates, raise `target_dte` and add for example
  `product_overrides: {expiration_rules: [{kind: friday, max_dte: 56}, {kind: third_friday, max_dte: 400}]}`.
- **XSP strikes are $1 = 10 SPX points**, so a 25-point debit spread isn't
  possible. `configs/xsp.yaml` uses 30 (20 also works).
- **Fees eat into the small products.** Defaults are Schwab's published
  rates plus estimated exchange fees: XSP ~$0.70, ES ~$2.85 and MES ~$2.45
  per contract. Schwab charges futures options a flat $2.25 per contract
  regardless of size, so a 4-leg MES condor costs ~$9.80 to open against a
  ~$8-9 average credit. Check your thinkorswim order confirmation and override
  with `product_overrides` (`fee_per_contract_open`, `fee_per_contract_close`).
- ES American-style early assignment is **not** simulated. ES serial-month
  options settle into the next quarterly future, and the backtest values that
  as cash at the futures price.

## Setup (Windows, PowerShell)

```powershell
cd condor_backtester
pip install -r requirements.txt
python scripts\fetch_market_data.py            # SPX open/close, VIX, VIX3M, 3m T-bill -> data\market.csv
```

`fetch_market_data.py` pulls free CSVs from CBOE and FRED. If CBOE blocks it,
it falls back to Yahoo (`pip install yfinance`).

## Run

```powershell
# one product
python -m condor_bt run --config configs\spx.yaml --market data\market.csv
python -m condor_bt run --config configs\mes.yaml --market data\market.csv

# all four side by side
python -m condor_bt compare --market data\market.csv --config configs\spx.yaml --config configs\es.yaml --config configs\mes.yaml --config configs\xsp.yaml

# spec parameter sweep (debit_delta x credit_delta x credit_width x min_credit = 108 runs)
python -m condor_bt sweep --config configs\spx.yaml --market data\market.csv

# model-risk sweep: how much do results change if the skew assumption is off?
python -m condor_bt sweep --grid surface --config configs\xsp.yaml --market data\market.csv

# change any setting without editing the YAML
python -m condor_bt run --config configs\mes.yaml --market data\market.csv --set target_dte=35 --set pricing=slippage
```

Outputs go to `results\<name>\`:

- `trades.csv`: entry, expiration, 4 strikes (plus XSP-native), deltas, credit, fees, exit, settlement, P&L, outcome bucket (above / partial_upper / tent / partial_lower / max_loss / early_exit / open), and skipped weeks with their reason.
- `daily.csv`: realized and mark-to-market P&L, open positions, sum of open max losses, modeled buying power.
- `equity.png`: cumulative P&L (realized and mark-to-market), drawdown, capital at risk.
- `summary.md` / `summary.json`: win rate, average win/loss, worst trade, total P&L, fees, mark-to-market and realized drawdown, max concurrent positions, worst-case aggregate loss, peak buying power, ROC and CAGR on Reg-T capital and on peak buying power, losses by expiration month, and SPX buy-and-hold over the same window.

## Weekly order helper (XSP)

Run it each Friday during market hours. It never places orders. It prints
the exact thinkorswim order for you to check and enter.

```powershell
pip install yfinance
python -m condor_bt.weekly plan        # this week's 4 strikes, deltas, credit, order + limit ladder
python -m condor_bt.weekly record 2026-12-31 732/729/690/680 0.15 --mid 0.14   # after a fill
python -m condor_bt.weekly status      # open positions, total risk, your fills vs mid
```

- Settings are in `configs/weekly_xsp.yaml`: deltas, widths (3 and 10 XSP
  points), `min_credit`, position and risk limits, and Schwab's $0.66 per-leg fee.
- `plan` skips the week, and says why, if no expiration is within 90 +/- 7
  days, the strikes overlap, the mid credit is below `min_credit`, or the
  entry would break `max_open_positions` / `max_total_risk`.
- The journal (`journal/xsp_journal.csv`) is what `status` and the risk
  limits use, so record every fill. `status` also turns your fills-vs-mid
  record into the `slippage_per_leg` to use in the backtest.
- Always compare the strikes and mid with thinkorswim before sending. Yahoo
  quotes can be delayed ~15 minutes, and if Yahoo has no XSP chain the helper
  uses SPX / 10, which is approximate.

### Schwab API (optional, live quotes with Schwab's deltas)

1. Create an app at developer.schwab.com ("Accounts and Trading Production"),
   callback URL `https://127.0.0.1:8182`. Approval can take a few days.
2. `pip install schwab-py`, then set the app's key and secret:
   ```powershell
   setx SCHWAB_API_KEY "your-app-key"
   setx SCHWAB_APP_SECRET "your-app-secret"
   ```
3. Open a new PowerShell and run `python -m condor_bt.weekly plan --source schwab`.
   The first run opens a browser to log in; the token is saved in `journal\`.
   After that, `source: auto` uses Schwab automatically.

## Option prices: model vs. real chains

**By default, option prices come from a model, not real quotes.** It uses
Black-76 on the SPX forward, with ATM vol from VIX/VIX3M and a put skew linear
in normalized moneyness (`condor_bt/pricing.py`). The broken-wing credit comes
almost entirely from skew, and the `--grid surface` sweep shows it: moving
`skew_put` from 0.36 to 0.45 roughly halves the average credit. Use model
results to compare products and parameters, not as an expected return.

To use real data, export end-of-day put quotes to a CSV and pass `--chains`:

```
date,expiration,strike,right,bid,ask[,delta][,iv][,underlying]
2024-03-01,2024-05-31,480,P,1.23,1.31,-0.198,0.162,512.4
```

Use native units (XSP as XSP, ES/MES in futures points). Provide every
trading day of each position's life, so it can be marked daily. If `delta`
is missing it is computed from `iv`, and if `iv` is missing too, it is
implied from the mid. Sources: ThetaData, ORATS, CBOE DataShop, Polygon.
With real chains, `pricing: natural` fills at bid/ask.

If you have even a few days of real chains, fit the model to them:

```powershell
python -m condor_bt calibrate --config configs\spx.yaml --market data\market.csv --chains my_chains.csv
```

Paste the printed `surface:` block into your YAML.

## Spec notes / decisions

- **Overlap rule:** the spec says both "L2 >= L3" and "skip if L3 is at or
  above L2". The default `overlap_rule: skip` uses the stricter reading
  (skip when L3 >= L2). `allow_equal` skips only when L3 > L2. `shift` moves
  L3 one strike below L2.
- **AM settlement** (SPX 3rd Friday, ES quarterlies) uses the SPX open as the
  SOQ proxy. Without an `open` column it falls back to the close and adds a warning.
- **ES/MES futures basis:** the option's underlying is priced as
  SPX x e^((r-q)t) to the quarterly future it settles against.
- **Buying power:** Reg-T is each position's max loss. The SPAN-like model
  is the worst loss over +/-8% spot and +/-25% vol scenarios, with a
  per-position floor ($500 ES, $50 MES), capped at the max loss. Peak
  aggregate buying power is reported next to the worst case where every
  open position hits max loss together.
- **Acceptance check (spec section 6):** reproducing the tastytrade numbers
  (~238 trades, 3 losses including a 2022 max loss) needs real SPX history.
  Run `configs/spx.yaml` on `data\market.csv` and compare. The model-priced
  run is only expected to be in the same ballpark.

## Tests

```powershell
python -m pytest -q
```

Covers the spec section 5 payoff numbers, calendars (holidays, MES listing
horizon, quarterlies), pricing, strike selection and ordering, credit filter,
slippage, exits, guards, the margin model, and a round trip that exports the
model chain to the real-chain CSV format and checks the backtest matches.
Tests run on `scripts/make_synthetic_market.py`, a **simulated** market
(not history) with an injected 2022-style bear and an April-2025-style crash.
