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

## Cross-ticker follow-up: NVDA and MSFT, with two added rules (run 2026-09-19)

Follow-up request: re-run against NVDA and MSFT (not just TSLA), gated by an
**IV-at-entry floor of 35%**, plus a **"thesis broken" stop** that closes both
legs immediately if the underlying trades 10 points below entry. See
`cross_ticker_audit.py` / `cross_ticker_output_2026-09-19.txt`.

No real IV data was available on this key (see Limitations), so the 35% floor
is applied to trailing 100-day **realized** vol as a disclosed proxy.

| symbol | spot | vol (IV proxy) | entry filter | debit | debit/width | reward:risk |
|---|---|---|---|---|---|---|
| TSLA | $364.27 | 52.8% | PASS | $2.34 | 23.4% | 3.26x |
| NVDA | $222.27 | 41.2% | PASS | $2.15 | 21.5% | 3.64x |
| MSFT | $493.78 | 38.4% | PASS | $2.46 | 24.6% | 3.07x |

All three clear both the 35% IV floor and the original debit ceiling today.
Two results from this run matter more than the pass/fail table, though:

**A. MSFT's pass is fragile — it's one earnings-gap day doing the work.**
MSFT's 100-day realized vol of 38.4% includes a single +14.4% gap day
(2026-07-29→07-30, an earnings move). Recomputed excluding just that one day,
MSFT's realized vol drops to **30.9% — below the 35% floor**, meaning MSFT
would have *failed* the entry filter on a window that didn't happen to
contain that gap. Trailing realized vol computed over a fixed lookback is
sensitive to whether a single outlier event falls inside the window, which
is a reason to prefer real forward-looking IV / IV rank (not available on
this API key) over a realized-vol proxy for this specific filter.

**B. The 10-point thesis-broken stop is not equivalent risk across tickers,
and it substantially changes the strategy's return profile everywhere.**
Compared to the same regimes without the stop (TSLA-only audit above), adding
it collapsed total-loss-rate (full max-loss outcomes) to roughly 0–1% almost
across the board — real tail protection. But it did that by cutting win rates
roughly in half and cutting mean returns substantially in every regime for
all three tickers (e.g. TSLA strong-bull mean return: +25.9% → +6.5%; win
rate 73.0% → 44.9%), because ordinary short-term noise routinely produces a
10-point pullback even on paths that go on to be winners. The size of that
effect differs by ticker because 10 points is a different fraction of each
stock's own volatility: it's under 1 daily-sigma move on TSLA and MSFT but
about 1.75 daily-sigma on NVDA (lower price, lower $ vol per point) — which
is why NVDA kept the highest win rates (44.8–59.1%) of the three after the
stop was added. A fixed-dollar stop is tighter, in effective terms, on a
higher-priced/higher-vol name than a lower-priced one. If the intent is
"exit when the thesis is genuinely wrong" rather than "exit on routine
chop," the stop distance should probably scale with the underlying (e.g., a
multiple of ATR or a % of spot) rather than staying a flat 10 points across
tickers.

Full regime-by-regime numbers (strong/moderate/weak bull, flat, bear, vol
crush, for both profit-taking rules) are in `cross_ticker_output_2026-09-19.txt`.

## Rule refinements: robust entry filter + stop-percentage sweep (run 2026-09-19)

Follow-up request: fix the entry filter's MSFT fragility, and re-test the
thesis-broken stop as a **percentage of entry price** instead of a flat $10,
swept across 2%/3%/4%/5%/6%. See `stop_pct_sweep.py` /
`stop_pct_sweep_output_2026-09-19.txt`.

**Entry filter fix.** Using *trimmed* realized vol (the 100-day sample with
its single largest-magnitude daily move excluded) instead of raw realized
vol resolves finding A above directly: MSFT drops to 30.9% and now correctly
**fails** the 35% floor on a basis that isn't one outlier event, while TSLA
(46.6%) and NVDA (39.1%) still pass on real, sustained volatility.

**Stop-percentage sweep — the real finding isn't which percentage, it's that
none of them help.** At every level tested (2% through 6%), for both TSLA
and NVDA, mean return is *worse* than having no stop at all (e.g. NVDA:
+10.7% no-stop vs +4.2% at the loosest 6% stop tested). The reason is
structural, not a tuning problem: a bull call spread's maximum loss is
**already capped at the debit paid**, whatever the stock does. The
"total_loss_rate" column with no stop (35–37%) isn't unbounded risk, it's
just "expired worthless" — already the worst case. A price stop, flat-dollar
or percentage, doesn't lower that floor; it just closes some trades early
that would have recovered, turning a subset of would-be wins into locked-in
partial losses. Recommendation: don't use a hard price/percentage stop as a
*risk*-management tool on this instrument, since the risk is already capped
by construction — if a stop is used at all, treat it as a *capital-efficiency*
rule (free up capital/attention from a clearly dead trade) rather than a loss
preventer, and prefer a technical thesis-invalidation trigger over an
arbitrary price/percentage level if a real early exit is wanted.

**NVDA vs TSLA, the two survivors.** NVDA has the better numbers on every
axis: cheaper relative debit (21.5% of width vs TSLA's 23.8%), better
reward:risk (3.64x vs 3.26x), and the better no-stop mean return/win rate
(+10.7%/63.9% vs +8.2%/63.1%) — consistent with NVDA clearing the 35%
signal-quality bar with less of TSLA's extra chop layered on top.

## Files

- `tsla_bull_call_spread_audit.py` — the original TSLA-only audit: real-data
  entry check (Section 1), Monte Carlo of both profit-taking rules across 6
  drift/vol regimes (Section 2), and entry-debit sensitivity (Section 3).
- `cross_ticker_audit.py` — the follow-up: same construction/Monte Carlo
  logic applied to TSLA, NVDA, and MSFT, with the 35% IV-entry floor and the
  10-point thesis-broken stop layered in.
- `data/tsla_daily_2026-09-18.json`, `data/nvda_daily_2026-09-18.json`,
  `data/msft_daily_2026-09-18.json` — raw Alpha Vantage `TIME_SERIES_DAILY`
  responses used for realized-vol calibration (real data, not simulated).
- `stop_pct_sweep.py` — the rule-refinement follow-up: trimmed-vol entry
  filter, close-all-@45% only (the recommended default), and a stop
  swept as a percentage of entry price (none/2%/3%/4%/5%/6%) across the
  same 6 regimes.
- `audit_output_2026-09-19.txt` — TSLA-only audit run output.
- `cross_ticker_output_2026-09-19.txt` — cross-ticker audit run output.
- `stop_pct_sweep_output_2026-09-19.txt` — stop-percentage sweep run output.

## Known limitations (disclosed, not hidden)

- No live option chain was available for any of the three tickers (Alpha
  Vantage `REALTIME_OPTIONS` and `HISTORICAL_OPTIONS` both returned
  premium-tier errors on this key); all strikes/debits are Black-Scholes
  theoretical values calibrated to each ticker's real trailing 100-day
  realized vol, not quoted market prices. Real bid/ask on names this liquid
  should track fairly close, but the quoted debit should always be checked
  against the live chain before entry.
- The 35% "IV" entry floor in the cross-ticker follow-up is applied to
  trailing realized vol, not true implied vol (unavailable on this key) —
  see finding A above for a concrete case (MSFT) where that substitution
  changes the entry decision depending on whether one outlier day is inside
  the lookback window. Real IV/IV-rank would not have this artifact.
- NVDA's earnings date could not be confirmed (Alpha Vantage
  `EARNINGS_CALENDAR` returned no entry in the next 3 months); MSFT's
  `EARNINGS_CALENDAR` call returned a malformed response and was not
  retried. Neither ticker's earnings-overlap risk has been verified the way
  TSLA's was — check both manually before entry.
- The 30-day base case in Section 2 is a disclosed assumption — the rule
  text doesn't specify DTE; Section 1 shows the same entry check across
  21/30/35/45/60 DTE.
- Regime drift assumptions (strong/moderate/weak bull, flat, bear, vol
  crush) are illustrative buckets for "how right was the signal," not a
  probability-weighted forecast for TSLA specifically.
