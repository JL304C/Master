#!/usr/bin/env python3
"""
Ross — the dispatcher.

Reads pending consensus events from the state store, dispatches them to
the user via Gmail SMTP (always) and Telegram (optional, if configured).
Marks each event dispatched.

Schedule: every 30 minutes. Idempotent — re-running with no pending
events is a no-op; a failed email is retried on the next run.

NEVER places trades. Output is informational. The human decides.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (
    log,
    mark_dispatched,
    pending_consensus,
    render_consensus,
    run_agent,
    send_email,
    send_telegram,
)


def main() -> int:
    pending = pending_consensus()
    if not pending:
        log("ross", "no pending consensus events")
        print("[ross] nothing to dispatch")
        return 0

    delivered = 0
    for row_id, ev in pending:
        body = render_consensus(ev)
        subject = f"[INSIDER] CONSENSUS {ev.direction} on {ev.ticker}"

        # Gmail SMTP — always required.
        try:
            send_email(subject, body)
            log("ross", f"email sent for consensus [{row_id}] {ev.ticker}")
        except Exception as exc:  # noqa: BLE001
            log("ross", f"email FAILED for [{row_id}]: {exc}")
            print(f"[ross] email FAILED for {ev.ticker}: {exc}")
            continue  # not marked dispatched — retried next run

        # Telegram — optional. Failure here doesn't block dispatch.
        if send_telegram(f"{subject}\n\n{body}"):
            log("ross", f"telegram sent for [{row_id}]")

        mark_dispatched(row_id)
        delivered += 1
        print(f"[ross] dispatched {ev.direction} {ev.ticker}")

    log("ross", f"delivered {delivered}/{len(pending)} pending events")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_agent("ross", main))
