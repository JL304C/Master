# DC Time Machine — Alpaca Paper Feasibility Probe

Tests whether Alpaca can carry the trade in
`dc_time_machine_strategy.md` (SPXW double calendar → risk-free iron condor)
**before** a full bot is built. **Paper trading only** — `paper=True` is
hard-coded in `dc_probe.py`.

The question it answers: **will Alpaca accept the "transformer" order** —
one 4-leg order that sells the back-month longs to close and buys 5-wide
front-month wings to open — as a single combo, at the risk-free credit?

## Setup (Windows, same as the GPC bot)

1. `pip install alpaca-py`
2. Copy `.env.example` to `.env` in this folder and put in your **paper** keys.
3. In the Alpaca paper dashboard, confirm options trading **level 3** is enabled
   (needed for multi-leg spreads).
4. Optional but recommended: the OPRA options data subscription. Without it,
   Alpaca serves the free *indicative* feed (delayed/modified quotes), so
   strikes and prices the probe computes will be rougher.

## Run it, in order, during market hours

| Step | Command | What it does | Places orders? |
|---|---|---|---|
| 1 | `python dc_probe.py check` | Account level, whether SPX/SPXW contracts and chain data (quotes, IV, greeks) come back, SPX estimated from put-call parity (Alpaca has no index quotes), and the calendar the strategy would open now, with debit, C_min and IV ratio | No |
| 2 | `python dc_probe.py open` | Opens **one** 1-lot double calendar as a single 4-leg order: starts at mid, concedes $0.05/min up to `--max-slip` (default 0.50) | Yes |
| 3 | `python dc_probe.py transform` | Submits the transformer at `C_min = D + 5 + fees`. Prints **ACCEPTED** or **REJECTED** with Alpaca's exact error. Add `--wait 60` to watch it for an hour | Yes |
| 4 | `python dc_probe.py status` | State, transformer order status, probe positions | No |
| 5 | `python dc_probe.py close` | Before the close: closes whatever is still held (calendar or condor) | Yes |

Spec section 5 says not to hold an untransformed calendar overnight, so if
the transformer hasn't filled by about 3:30 pm ET, run `close`.

**To test acceptance without waiting for the calendar to gain value:** the
transformer is a limit order at C_min, so it just sits working until it
fills. Acceptance vs. rejection shows up the moment you submit it, which is
the real test. `status` later tells you whether it filled.

## What to send back

`dc_probe_log.jsonl` (created here) has every step. The key lines:
- from `check`: `chain_underlying_that_worked`, `chain_contracts_with_iv`,
  `chain_contracts_with_delta`, `spx_forward_from_parity` vs `spy_x10`
- `open_calendar_filled` (or `_rejected` / `_gave_up`)
- `transform_ACCEPTED` or `transform_REJECTED` (with the error text)

## Assumptions to verify from the results

- `FEE_PER_LEG_CONTRACT = 0.70` is a conservative placeholder for SPX
  exchange/OCC/regulatory fees; replace with what the fills actually show.
- Alpaca's paper simulator fills combos more easily than real SPXW market
  makers will. Treat paper fills as optimistic.
- A 1-lot SPX double calendar typically costs roughly $500–$2,000 in debit
  (strike/IV dependent). That's paper money here, but note it against the
  $26k cap before going live.
