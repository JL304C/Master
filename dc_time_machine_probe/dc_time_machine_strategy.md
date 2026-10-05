# DC Time Machine Strategy Specification
Double Calendar → Risk-Free Iron Condor (standard 5-wide method)

Source: Steve Bernitsch (Navigation Trading), interviewed on Theta Profits. This spec covers only the standard transformation (equal 5-point wings on both sides). Variations such as unequal wings, broken-wing condors, and verticals are out of scope.

---

## 1. Concept

1. Open a **double calendar** (put calendar below price + call calendar above price) for a debit.
2. Wait until the calendar gains enough value (typically 5–10% profit).
3. In **one combo order**, close the back-month long options and buy 5-wide protective wings in the front month. This converts the position into an **iron condor in the front expiration**.
4. If the net credit from the transformer order is at least `original debit + wing width`, the iron condor cannot lose money at expiration. Its worst outcome is zero or a small profit.
5. Hold to expiration (SPX cash-settles), or scale out on expiration day if price threatens the profit zone.

Goal: remove risk and broker buying-power requirements as fast as possible, then redeploy capital into new double calendars.

---

## 2. Instrument

| Parameter | Value |
|---|---|
| Underlying | SPX (SPXW weeklies) |
| Why | European style, cash-settled (no assignment, no shares), U.S. Section 1256 tax treatment |
| Alternative | Any underlying with liquid options. American-style options (SPY, single stocks) carry assignment risk, which breaks the "risk-free" assumption. |

---

## 3. Entry: Double Calendar Construction

### 3.1 Expirations
- **Front (short) expiration:** an expiration in the **following week**, roughly 6–15 DTE.
  - Example: on May 26, use expirations starting June 1.
- **Back (long) expiration:** **1 to 4 days after the front expiration**. Steve rarely goes more than 3–4 days apart.
  - Common: front + 1 day (e.g., sell June 4, buy June 5).
  - Favorite: **sell Friday, buy following Monday**. This pair is usually in backwardation (front IV > back IV), which favors the trade.

### 3.2 Strikes
- **Put calendar:** strike below current price at about **30–40 delta**.
- **Call calendar:** strike above current price at about **30–40 delta**.
- Each calendar uses the **same strike** in both expirations.
- Closer strikes transform to risk-free faster but give a narrower iron condor. Wider strikes (15–20 delta) give a wider condor but take longer to transform. Default: 30–40 delta.

### 3.3 Legs (per contract)
| Leg | Action | Type | Expiration | Strike |
|---|---|---|---|---|
| 1 | SELL to open | Put | Front | Put strike (Kp) |
| 2 | BUY to open | Put | Back | Kp |
| 3 | SELL to open | Call | Front | Call strike (Kc) |
| 4 | BUY to open | Call | Back | Kc |

Enter as a single 4-leg order for a **net debit (D)**. Record D per contract.

### 3.4 Timing
- Wait a few minutes after the market open before entering.
- Volatility condition (preferred, not required):
  - Compute `IV_ratio = front_IV / back_IV` for the chosen expiration pair, updated every minute.
  - Prefer entry **after a spike in IV_ratio**. IV is mean-reverting, and a falling ratio after entry (front IV contracting faster than back IV) produces profit.
  - A rising ratio after entry hurts the position.
  - Optional context: check whether the ratio has been declining over 5-day and 20-day windows.
  - Optional scanner: scan all front expirations from 0–30 DTE with a chosen front/back gap (e.g., 1, 3, or 5 days) and flag pairs with a minimum intraday drop or rise in IV_ratio.
- This is context, not prediction. Steve's "Flux" tool is private to Navigation Trading members, so the IV_ratio must be calculated from your own option chain data.

---

## 4. Transformation: Double Calendar → Iron Condor

### 4.1 Transformer order (single 4-leg combo order)
| Leg | Action | Type | Expiration | Strike |
|---|---|---|---|---|
| 1 | SELL to close | Put | Back | Kp |
| 2 | SELL to close | Call | Back | Kc |
| 3 | BUY to open | Put | Front | Kp − W |
| 4 | BUY to open | Call | Front | Kc + W |

- **W = wing width = 5 points** (standard).
- The front-month short put at Kp and short call at Kc stay open.
- Resulting position: front-expiration iron condor
  - Long put Kp−W / short put Kp / short call Kc / long call Kc+W.

### 4.2 Risk-free price rule
```
Minimum transformer credit C_min = D + W
```
- At `C = D + W`: the outcome outside the short strikes is exactly $0 (breakeven).
- At `C > D + W`: the extra is locked-in profit even in the worst case.

### 4.3 Resulting P/L at expiration (per contract, ×100 multiplier)
```
Net credit locked in   N = C − D
Max profit             = N × 100            (SPX settles between Kp and Kc)
Worst case             = (N − W) × 100      (SPX settles beyond either wing)
```

Worked example (20 contracts, from the interview):
- Double calendar debit D = $10.10
- Transformer credit C = $15.25 (Kc=7560→wing 7565, Kp=7485→wing 7480, W=5)
- N = 15.25 − 10.10 = 5.15
- Max profit = 5.15 × 100 × 20 = **$10,300**
- Worst case = (5.15 − 5.00) × 100 × 20 = **$300 profit**
- Had C been $15.10 (= D + W), the worst case would be exactly $0.

### 4.4 Order placement options
- **Option A (unattended):** immediately after the double calendar fills, place the transformer order as a working limit order at `D + W` (or slightly higher) and let it fill whenever the calendar has gained enough.
- **Option B (scaled):** if profit comes in quickly, transform **half** the contracts at about `D + W`, then work the remaining half later at a **higher** credit. Any credit above `D + W` adds directly to both the worst-case profit and the max profit.

### 4.5 Typical time to transform
- No fixed timing. Extreme volatility: as fast as 7–20 minutes (with higher risk while untransformed).
- More typical: entered near the open, transformed in the afternoon (e.g., about 1.5 hours before close).
- Some days the calendar never gains enough to transform.

---

## 5. End-of-Day Handling (Untransformed Positions)

If the transformer order has not filled near the close:
1. **Default:** close the double calendar for whatever small profit or loss it has, to avoid overnight risk. Re-enter the next day.
2. **Alternative:** close part of the position (e.g., 10 of 20 contracts), and hold the rest overnight to try transforming the next day.

Steve prefers not to hold untransformed risk overnight.

---

## 6. Exit Rules (After Transformation)

1. **Default:** hold to expiration. The SPX iron condor cash-settles. Some will expire at max profit (inside Kp–Kc), some at the minimum locked-in profit.
2. Decisions are almost always made on **expiration day**, not before.
3. On expiration day, if price action threatens to move outside the max-profit zone (e.g., sudden news-driven move), **scale out**: close half the contracts, reassess, then close the rest to bank current profit rather than hope for max profit.
4. The position can be closed at any time to book current profit.
5. No stop-loss is needed once transformed, because there is no loss at expiration.

---

## 7. Portfolio Behavior

- Run continuously: open new double calendars across different expiration cycles and price levels, transform them, and build a ladder of risk-free iron condors.
- Transformation frees broker buying power, which is redeployed into new double calendars.

---

## 8. Parameters Not Specified in the Interview

The automated system needs decisions on these; the source does not provide values:
- Position size per trade and maximum concurrent untransformed calendars.
- Stop-loss for an **untransformed** double calendar (it carries real risk before transformation).
- Exact IV_ratio spike threshold for entry and scanner thresholds.
- Exact end-of-day cutoff time and the threshold for closing versus holding overnight.
- Quantitative expiration-day scale-out trigger (e.g., distance from short strike).
- Order-repricing logic if fills are slow.

---

## 9. Implementation Caveats

- **Commissions and fees** are not included in `D + W`. Add total round-trip fees per contract to C_min so the worst case is truly ≥ $0 after costs.
- "Risk-free" applies **at expiration**. Before expiration, the condor's mark-to-market value can show a loss if closed early.
- The transformer order mixes two expirations and both closing and opening legs. Verify that the broker API (e.g., Schwab Trader API) accepts this as a single custom 4-leg order. Never leg into it separately; legging creates fill risk.
- Use PM-settled SPXW contracts so settlement matches the expiration-day close.
- Calendar spread risk before transformation includes large price moves and front-month IV expanding faster than back-month IV.
- Steve also builds on Thinkorswim's risk-profile graph for visual confirmation. An automated system should compute the post-transformation P/L at expiration (Section 4.3) and verify `worst case ≥ 0` before sending the order.
