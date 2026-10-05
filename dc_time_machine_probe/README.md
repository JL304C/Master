# DC Time Machine — Alpaca Paper Feasibility Probe

Tests whether Alpaca can carry the trade in
`dc_time_machine_strategy.md` (double calendar → risk-free iron condor)
**before** a full bot is built. **Paper trading only** — `paper=True` is
hard-coded in `dc_probe.py`.

The question it answers: **will Alpaca accept the "transformer" order** —
one 4-leg order that sells the back-month longs to close and buys $1-wide
front-month wings to open — as a single combo, at the risk-free credit?

## Why SPY, not SPX

The first live run (2026-10-05) on SPXW was rejected by Alpaca:

```
HTTP 422 {"code":42210000,"message":"European-style option legs in a
multi-leg order must have the same expiration date"}
```

Every index option on Alpaca (SPX, SPXW, XSP, ...) is European-style, so
neither the calendar nor the transformer can go in as one order there. The
probe now runs on **SPY** (American-style), with these differences from the
spec:

- **Wing width W = $1** (SPY strikes are $1 apart); risk-free rule is still
  `credit ≥ D + W + fees`.
- **Early assignment is possible.** Low risk for ~35-delta OTM shorts, higher
  if price runs through a short strike, especially before an ex-dividend date.
- **SPY settles in shares.** The condor must be **closed on expiration day**,
  never left to expire. `status` warns when that day arrives.
- **No Section 1256 (60/40) tax treatment.**
- One SPY contract is ~1/10 the size of SPX: a calendar costs roughly $50–$100.

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
| 1 | `python dc_probe.py check` | Account level, whether SPY contracts and chain data (quotes, IV, greeks) come back, and the calendar the strategy would open now, with debit, C_min and IV ratio | No |
| 2 | `python dc_probe.py open` | Opens **one** 1-lot double calendar as a single 4-leg order: starts at mid, concedes $0.01/min up to `--max-slip` (default 0.05) | Yes |
| 3 | `python dc_probe.py transform` | Submits the transformer at `C_min = D + 1 + fees`. Prints **ACCEPTED** or **REJECTED** with Alpaca's exact error. Add `--wait 60` to watch it for an hour | Yes |
| 4 | `python dc_probe.py status` | State, transformer order status, probe positions | No |
| 5 | `python dc_probe.py close` | Closes whatever is still held (calendar or condor). Use it before the close on any day the transformer hasn't filled, and on the condor's expiration day before 3:45 pm ET | Yes |

Spec section 5 says not to hold an untransformed calendar overnight, so if
the transformer hasn't filled by about 3:30 pm ET, run `close`.

**To test acceptance without waiting for the calendar to gain value:** the
transformer is a limit order at C_min, so it just sits working until it
fills. Acceptance vs. rejection shows up the moment you submit it, which is
the real test. `status` later tells you whether it filled.

## What to send back

`dc_probe_log.jsonl` (created here) has every step. The key lines:
- from `check`: `chain_contracts_with_iv`, `chain_contracts_with_delta`,
  `forward_from_parity` vs `spy_quote_mid`
- `open_calendar_filled` (or `_rejected` / `_gave_up`)
- `transform_ACCEPTED` or `transform_REJECTED` (with the error text)

## Assumptions to verify from the results

- `FEE_PER_LEG_CONTRACT = 0.05` is a conservative placeholder for SPY
  regulatory/clearing fees over 12 legs (open, transform, close at expiry);
  replace with what the fills actually show.
- Alpaca's paper simulator fills combos more easily than real SPXW market
  makers will. Treat paper fills as optimistic.
