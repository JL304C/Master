#!/usr/bin/env python3
"""
Sophie — the consensus analyst.

Reads the rolling 7-day window of scout signals from the state store.
Fires a CONSENSUS event when >= MIN_AGREE different scouts agree on the
same ticker + direction within the window.

Each consensus fires once: Sophie only fires again for the same ticker +
direction inside the window if a scout that wasn't part of it joins.

Schedule: every 30 minutes (light-touch — just reads + writes the DB).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (
    NEUTRAL,
    ConsensusEvent,
    Signal,
    log,
    read_window,
    recent_consensus,
    record_consensus,
    run_agent,
)

MIN_AGREE = int(os.environ.get("SOPHIE_MIN_AGREE", "3"))
WINDOW_DAYS = int(os.environ.get("SOPHIE_WINDOW_DAYS", "7"))


def main() -> int:
    signals = read_window(days=WINDOW_DAYS)
    if not signals:
        log("sophie", "no signals in window — skipping")
        print("[sophie] no signals in window")
        return 0

    # Latest signal per scout per (ticker, direction). Signals arrive
    # newest first, so the first one seen wins.
    by_key: dict[tuple[str, str], dict[str, Signal]] = {}
    for s in signals:
        if s.direction == NEUTRAL:
            continue
        by_key.setdefault((s.ticker, s.direction), {}).setdefault(s.scout, s)

    fired = 0
    for (ticker, direction), latest in by_key.items():
        scouts = sorted(latest)
        if len(scouts) < MIN_AGREE:
            continue
        if any(set(scouts) <= prior for prior in recent_consensus(ticker, direction, WINDOW_DAYS)):
            continue  # already told the user about this exact agreement
        ev = ConsensusEvent(
            ticker=ticker,
            direction=direction,
            scouts=scouts,
            reasons=[f"{sc}: {latest[sc].reason}" for sc in scouts],
            timestamp=datetime.now(timezone.utc),
        )
        row_id = record_consensus(ev)
        log(
            "sophie",
            f"CONSENSUS [{row_id}] {direction} {ticker} ({len(scouts)} scouts: {', '.join(scouts)})",
        )
        print(f"[sophie] CONSENSUS {direction} {ticker} — {len(scouts)} scouts agree")
        fired += 1

    if fired == 0:
        log("sophie", f"no new consensus (min={MIN_AGREE}, window={WINDOW_DAYS}d)")
        print(f"[sophie] no new consensus (need ≥{MIN_AGREE} scouts agreeing)")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_agent("sophie", main))
