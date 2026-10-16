# SOXL weekly put backtest ("double-out expected move")

Backtest of the SOXL weekly cash-secured put strategy and its wheel follow-through, run on real historical SOXL option quotes.

- `fetch.py` downloads, for every Friday (or the last trading day of the week), one minute of SOXL option bid/ask from Databento OPRA (`cbbo-1m`, 15:45 ET) and the SOXL price. It caches these in `cache/`. Databento cost is about $2.
- `sim.py` runs the 4 variants (premium rule A/B × round down/nearest), the nearer-expiration-EM comparison, a monthly version, and buy-and-hold.
- `results.txt` holds the output of the run on 2026-10-16.

Assumptions:
- Sells fill at the bid, with $0.65 commission per contract.
- EM = spot × ATM IV of the reference expiration × √(DTE/365) × 0.798.
- An option settles against the 15:45 ET price on expiration Friday, or against the daily close when it expires on another day.
- Capital compounds: every put is fully cash-secured against the whole account.
- Event flags cover only NVDA earnings and jobs-report Fridays.

SOXL listed only monthly options until September 2021, so the weekly test starts at 2021-09-24. The monthly run (30-DTE put, EM from the ~60-DTE expiration) covers 2018-05 to 2026-10, which includes the 2020 crash.
