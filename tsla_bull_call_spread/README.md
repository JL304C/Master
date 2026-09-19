# TSLA Bull Call Spread — Rule Audit

Audits the bull-call-debit-spread rule set (below) against TSLA's **real** price
history and Black-Scholes-priced option chain (Alpha Vantage free tier — real
options-chain and realtime-quote endpoints are premium-gated on this key, so
the chain itself is theoretical, calibrated to TSLA's real ~53% realized vol;
see `tsla_bull_call_spread_audit.py` for the exact math).

## The rules being audited

> Use a bull call debit spread only when the system has a strong, time-bounded
> bullish signal. For a chosen underlying and one shared expiration, buy one
> call near 30 delta, then simultaneously sell one call exactly 10 strike
> points higher; place the order as a vertical spread at a net debit using a
> limit price near the midpoint. Accept entries only when the debit is at or
> below 25%–30% of the $10-wide spread — ideally ≤$2.50, never above $3.00 per
> share before fees — so the defined maximum loss is the debit and potential
> expiration profit remains at least roughly 2.3–3x the amount at risk. At
> expiration, breakeven is long strike plus debit; maximum profit is spread
> width minus debit and occurs at or above the short strike.
>
> Size each trade so a complete loss of the net debit is acceptable
> ("position for zero"); do not add to, roll, or adjust losing positions, and
> close both legs together. Automate profit-taking as: close all at 40%–50%
> return, or at 100% return sell half the contracts and hold the remainder for
> a target near the short strike; consider closing before expiration to avoid
> assignment/expiration uncertainty.

## What the audit found (run 2026-09-19, see `audit_output_2026-09-19.txt`)

**1. The debit ceiling is not the binding constraint on TSLA right now.**
At TSLA's real ~53% annualized realized vol and ~$364 spot, the theoretical
30-delta/10-wide call spread prices at **~22–24% of width across every DTE
tested (21/30/35/45/60)** — comfortably under the $2.50 ideal and $3.00 hard
cap, with reward:risk of 3.2x–3.5x. This is a property of the $10 width being
small relative to TSLA's price/vol, not evidence the strategy is a good idea
today — it just means, unusually, the rule's own risk math doesn't reject a
TSLA entry on price alone. On a lower-priced or lower-vol underlying the same
$10 width eats a much bigger share of the premium and the debit cap becomes
the real gate.

**2. The rule's paragraph-1 gate (a "strong, time-bounded bullish signal")
is undefined and cannot be backtested from price data alone.** This audit
can only test what happens *after* a signal fires, across a spread of
post-entry drift regimes standing in for "the signal was right" vs. "it
wasn't." That is a real gap in the rules as given, not a gap in this audit —
flagging it rather than quietly assuming a signal generator.

**3. Section 2 (Monte Carlo, 2,000 paths per regime) — the two profit-taking
rules are not equivalent, and "sell half at 100%" is the weaker one here:**
| Regime | close-all @45%: mean / win-rate | sell-half @100%: mean / win-rate |
|---|---|---|
| strong bull +60%/yr | +25.9% / 73.0% | +39.9% / 39.6% |
| moderate bull +30%/yr | +16.5% / 68.0% | +22.6% / 34.1% |
| flat | +6.8% / 62.4% | −2.8% / 26.0% |
| bear −25%/yr | +4.1% / 61.3% | −7.2% / 23.9% |
| vol crush (IV −40%) | −13.3% / 49.1% | −1.3% / 29.8% |

"Close all at 40–50%" wins on win-rate and mean return in every regime except
the strongest bull case, where letting the back half run to the short strike
pays off with a much bigger mean but at a much lower hit rate (~40%) and
roughly 2x the return volatility. Read literally, the two rules in the prompt
are alternatives for different regime confidence, not one universally-better
choice — this data backs that framing: only reach for "sell half, hold the
rest" when the bullish signal is specifically calling for a large, fast move,
not as a default.

**4. A 45-DTE-or-longer expiration spans TSLA's next confirmed earnings date
(2026-10-28, per Alpha Vantage `EARNINGS_CALENDAR`).** The rule set doesn't
mention earnings at all. A debit spread's capped risk means an adverse gap
can't exceed the debit either way, but an earnings-adjacent expiration adds
a binary, non-technical event inside an otherwise "time-bounded bullish
signal" window — worth a deliberate choice (avoid spanning it, or size
knowing it's there), not a silent default.

**5. Loss frequency is real and matches the rule's own framing.** "Position
for zero" is not a hedge against a real outcome: even in the *strong bull*
regime, ~27–41% of simulated trades hit total loss of the debit (higher under
the sell-half rule, since the retained half rides all the way to expiration
without the 40–50% floor). Flat/bear/vol-crush regimes push total-loss rates
to 50–58%. The strategy's edge is entirely contingent on the paragraph-1
signal being right often enough and by enough to clear these odds — which,
per point 2, this audit cannot verify.

## Files

- `tsla_bull_call_spread_audit.py` — the audit: real-data entry check
  (Section 1), Monte Carlo of both profit-taking rules across 6 drift/vol
  regimes (Section 2), and entry-debit sensitivity (Section 3).
- `data/tsla_daily_2026-09-18.json` — raw Alpha Vantage `TIME_SERIES_DAILY`
  response used for the realized-vol calibration (real data, not simulated).
- `audit_output_2026-09-19.txt` — full run output.

## Known limitations (disclosed, not hidden)

- No live TSLA option chain was available (Alpha Vantage `REALTIME_OPTIONS`
  and `HISTORICAL_OPTIONS` both returned premium-tier errors on this key);
  Section 1's strikes/debit are Black-Scholes theoretical values calibrated
  to TSLA's real trailing 100-day realized vol, not quoted market prices.
  Real bid/ask on a name this liquid should track fairly close, but the
  quoted debit should always be checked against the live chain before entry.
- The 30-day base case in Section 2 is a disclosed assumption — the rule
  text doesn't specify DTE; Section 1 shows the same entry check across
  21/30/35/45/60 DTE.
- Regime drift assumptions (strong/moderate/weak bull, flat, bear, vol
  crush) are illustrative buckets for "how right was the signal," not a
  probability-weighted forecast for TSLA specifically.
