#!/usr/bin/env python3
"""
Janet — the portfolio-drift accountant.

Compares your current portfolio against your target allocation and flags
every position that has drifted >= 5 percentage points:
  overweight  → BEARISH on that ticker (consider trimming)
  underweight → BULLISH on that ticker (consider adding)

Schedule: daily, 17:00 local time.

Reads two files you create from the examples in config/:
  config/portfolio_target.json   {ticker: target_pct}
  config/portfolio_current.json  {ticker: current_value_usd}
Janet skips (no signal) until both exist.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (
    BEARISH,
    BULLISH,
    CONFIG,
    Signal,
    log,
    record_signals,
    run_agent,
)

TARGET = CONFIG / "portfolio_target.json"
CURRENT = CONFIG / "portfolio_current.json"

DRIFT_THRESHOLD_PP = 5.0  # percentage-point drift before Janet flags


def drift_signals(target: dict[str, float], current_value: dict[str, float]) -> list[Signal]:
    total = sum(current_value.values()) or 1.0
    current_pct = {k.upper(): 100.0 * v / total for k, v in current_value.items()}
    sigs = []
    for ticker, target_pct in target.items():
        ticker = ticker.upper()
        cur = current_pct.get(ticker, 0.0)
        drift = cur - target_pct  # +drift = overweight, -drift = underweight
        if abs(drift) < DRIFT_THRESHOLD_PP:
            continue
        sigs.append(
            Signal(
                scout="janet",
                ticker=ticker,
                direction=BEARISH if drift > 0 else BULLISH,
                confidence=min(5, 1 + int(abs(drift) // 5)),
                reason=(
                    f"{ticker} drifted {drift:+.1f}pp (target {target_pct:.1f}% → "
                    f"current {cur:.1f}%) — {'trim' if drift > 0 else 'add'}"
                ),
                raw=json.dumps({"current_pct": current_pct}),
            )
        )
    return sigs


def main() -> int:
    if not TARGET.exists() or not CURRENT.exists():
        print("[janet] portfolio_target.json / portfolio_current.json not set up — skipping")
        log("janet", "portfolio files missing, skipped")
        return 0
    sigs = drift_signals(json.loads(TARGET.read_text()), json.loads(CURRENT.read_text()))
    if not sigs:
        print("[janet] portfolio within tolerance")
        log("janet", "portfolio within tolerance")
        return 0
    record_signals("janet", sigs)
    return 0


if __name__ == "__main__":
    raise SystemExit(run_agent("janet", main))
