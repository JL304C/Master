"""
common.py — shared foundation for the 7 Insider agents.

Used by Eddie / Maggie / Frank / Maya / Janet (scouts), Sophie (consensus),
and Ross (dispatcher). Provides:

  - http_get() / sec_headers()   Rate-limited HTTP with retries (stdlib only)
  - ask_claude_json()            Claude call with a JSON-schema-constrained reply
  - record_signals()             Write scout signals to the state store
  - read_window()                Read the rolling window of scout signals
  - record_consensus() etc.      Consensus events for Sophie / Ross
  - kv_get() / kv_set()          Small key-value table (what each scout has seen)
  - send_email() / send_telegram()
  - log() / run_agent()

Design rule: scouts FETCH real data with code, and decide signals with
code. Claude is only asked to judge text that was actually fetched (Frank's
Fed speeches) — it is never asked to "go search" something it cannot reach.

State lives at <ROOT>/.state/state.db (SQLite), where ROOT is the
insider_routines/ folder (override with INSIDER_HOME).
Config lives at <ROOT>/.env (read at startup via python-dotenv).

Pass --dry-run to any scout to print its signals without saving them.
"""

from __future__ import annotations

import json
import os
import smtplib
import sqlite3
import sys
import textwrap
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable

try:
    from dotenv import load_dotenv  # type: ignore
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "Missing dependency: python-dotenv. Install with "
        "`python3 -m pip install -r requirements.txt`.\n"
    )
    raise


# ── Paths ────────────────────────────────────────────────────────────────────

ROOT = Path(os.environ.get("INSIDER_HOME") or Path(__file__).resolve().parent.parent)
CONFIG = ROOT / "config"
STATE = ROOT / ".state"
LOGS = STATE / "logs"
DB_PATH = STATE / "state.db"
ENV_PATH = ROOT / ".env"

# Load env on import — every agent boots through this module.
if ENV_PATH.exists():
    load_dotenv(ENV_PATH)

DRY_RUN = "--dry-run" in sys.argv
FORCE = "--force" in sys.argv


# ── Models ───────────────────────────────────────────────────────────────────

DEFAULT_MODEL = os.environ.get("INSIDER_MODEL", "claude-opus-5-5")
DEFAULT_EFFORT = os.environ.get("INSIDER_EFFORT", "medium")


# ── Direction taxonomy ───────────────────────────────────────────────────────

BULLISH = "BULLISH"
BEARISH = "BEARISH"
NEUTRAL = "NEUTRAL"
DIRECTIONS = (BULLISH, BEARISH, NEUTRAL)


# ── Dataclasses ──────────────────────────────────────────────────────────────


@dataclass
class Signal:
    """A single scout's structured output."""

    scout: str
    ticker: str  # ticker, asset symbol, or "MACRO"
    direction: str  # BULLISH | BEARISH | NEUTRAL
    confidence: int  # 1–5
    reason: str  # one-line plain-English reason
    raw: str  # source data behind the signal, for audit


@dataclass
class ConsensusEvent:
    """Sophie's output when ≥3 scouts agree."""

    ticker: str
    direction: str
    scouts: list[str]
    reasons: list[str]
    timestamp: datetime


# ── State store ──────────────────────────────────────────────────────────────


def _ensure_dirs() -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)


def _conn() -> sqlite3.Connection:
    _ensure_dirs()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scout TEXT NOT NULL,
            ticker TEXT NOT NULL,
            direction TEXT NOT NULL,
            confidence INTEGER NOT NULL,
            reason TEXT NOT NULL,
            raw TEXT NOT NULL,
            ts TEXT NOT NULL
        )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS consensus (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            direction TEXT NOT NULL,
            scouts TEXT NOT NULL,
            reasons TEXT NOT NULL,
            ts TEXT NOT NULL,
            dispatched INTEGER DEFAULT 0
        )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS kv (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts)""")
    return conn


def record_signals(scout: str, sigs: list[Signal]) -> None:
    """Persist a scout's signals (or just print them under --dry-run)."""
    for sig in sigs:
        line = f"{sig.ticker} {sig.direction} conf={sig.confidence} :: {sig.reason}"
        print(f"[{scout}] {line}")
        if not DRY_RUN:
            log(scout, f"signal: {line}")
    if DRY_RUN:
        print(f"[{scout}] --dry-run: {len(sigs)} signal(s) NOT saved")
        return
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as c:
        c.executemany(
            "INSERT INTO signals (scout, ticker, direction, confidence, reason, raw, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (s.scout, s.ticker, s.direction, s.confidence, s.reason, s.raw, now)
                for s in sigs
            ],
        )


def read_window(days: int = 7) -> list[Signal]:
    """Return all scout signals in the last `days` days, newest first."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _conn() as c:
        rows = c.execute(
            "SELECT scout, ticker, direction, confidence, reason, raw "
            "FROM signals WHERE ts >= ? ORDER BY ts DESC, id DESC",
            (cutoff,),
        ).fetchall()
    return [Signal(*r) for r in rows]


def recent_consensus(ticker: str, direction: str, days: int) -> list[set[str]]:
    """Scout sets of consensus events already fired for this key in the window."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _conn() as c:
        rows = c.execute(
            "SELECT scouts FROM consensus WHERE ticker = ? AND direction = ? AND ts >= ?",
            (ticker, direction, cutoff),
        ).fetchall()
    return [set(json.loads(r[0])) for r in rows]


def record_consensus(ev: ConsensusEvent) -> int:
    """Write a consensus event. Returns the row id for Ross to track dispatch."""
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO consensus (ticker, direction, scouts, reasons, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                ev.ticker,
                ev.direction,
                json.dumps(ev.scouts),
                json.dumps(ev.reasons),
                ev.timestamp.isoformat(),
            ),
        )
        return int(cur.lastrowid or 0)


def pending_consensus() -> list[tuple[int, ConsensusEvent]]:
    """Ross reads this — events not yet dispatched."""
    with _conn() as c:
        rows = c.execute(
            "SELECT id, ticker, direction, scouts, reasons, ts FROM consensus WHERE dispatched = 0"
        ).fetchall()
    return [
        (
            int(r[0]),
            ConsensusEvent(
                ticker=r[1],
                direction=r[2],
                scouts=json.loads(r[3]),
                reasons=json.loads(r[4]),
                timestamp=datetime.fromisoformat(r[5]),
            ),
        )
        for r in rows
    ]


def mark_dispatched(row_id: int) -> None:
    with _conn() as c:
        c.execute("UPDATE consensus SET dispatched = 1 WHERE id = ?", (row_id,))


def kv_get(key: str, default: str | None = None) -> str | None:
    with _conn() as c:
        row = c.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def kv_set(key: str, value: str) -> None:
    """Remember what a scout has processed. Skipped under --dry-run."""
    if DRY_RUN:
        return
    with _conn() as c:
        c.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


# ── HTTP ─────────────────────────────────────────────────────────────────────

# Minimum seconds between requests per host. SEC allows 10 req/s; Etherscan's
# free tier 3 req/s; OpenFIGI without a key 25 req/min.
_MIN_INTERVAL = {
    "www.sec.gov": 0.12,
    "data.sec.gov": 0.12,
    "api.etherscan.io": 0.4,
    "api.openfigi.com": 2.5,
}
_last_call: dict[str, float] = {}


class HTTPNotFound(Exception):
    pass


def http_get(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout: float = 30,
    retries: int = 4,
    not_found: tuple[int, ...] = (404,),
) -> bytes:
    """GET (or POST when `data` is set) with per-host throttling and retries.

    Retries 429 / 5xx / network errors with 2s, 4s, 8s, 16s backoff.
    Raises HTTPNotFound on 404 (or any code in `not_found`) so callers can
    treat "not published yet" cleanly.
    """
    host = urllib.parse.urlparse(url).netloc
    req = urllib.request.Request(url, data=data, headers=headers or {})
    for attempt in range(retries + 1):
        wait = _MIN_INTERVAL.get(host, 0) - (time.monotonic() - _last_call.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        _last_call[host] = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in not_found:
                raise HTTPNotFound(url) from exc
            if exc.code == 403 and host.endswith("sec.gov"):
                raise RuntimeError(
                    "SEC returned 403. SEC requires a User-Agent with your name and "
                    "email — set SEC_USER_AGENT in .env (e.g. 'Jane Doe jane@gmail.com')."
                ) from exc
            if exc.code != 429 and exc.code < 500 or attempt == retries:
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == retries:
                raise
        time.sleep(2 ** (attempt + 1))
    raise RuntimeError("unreachable")


def http_json(url: str, **kw: Any) -> Any:
    return json.loads(http_get(url, **kw).decode("utf-8"))


def sec_headers() -> dict[str, str]:
    """SEC fair-access policy: identify yourself with a name + email."""
    ua = os.environ.get("SEC_USER_AGENT") or (
        f"InsiderRoutines {os.environ['GMAIL_USER']}" if os.environ.get("GMAIL_USER") else ""
    )
    if not ua:
        raise RuntimeError(
            "Set SEC_USER_AGENT (your name + email) or GMAIL_USER in .env — "
            "SEC blocks anonymous requests."
        )
    return {"User-Agent": ua}


# ── Claude ───────────────────────────────────────────────────────────────────


def ask_claude_json(
    system: str,
    user: str,
    schema: dict[str, Any],
    *,
    model: str | None = None,
    effort: str | None = None,
    max_tokens: int = 16000,
) -> dict[str, Any]:
    """One Claude call whose reply is constrained to `schema`. Returns the parsed dict."""
    from anthropic import Anthropic  # imported lazily: only Frank needs it

    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        raise RuntimeError("ANTHROPIC_API_KEY not set. Add it to .env")
    client = Anthropic(http_client=_http_client())
    try:
        msg = _create(client, system, user, schema, model, effort, max_tokens)
    except Exception as exc:
        # The SDK's "Connection error." hides the real cause; surface it.
        root = exc
        while root.__cause__ is not None:
            root = root.__cause__
        if root is exc:
            raise
        raise RuntimeError(f"{exc} Cause: {type(root).__name__}: {root}") from exc
    if msg.stop_reason == "refusal":
        raise RuntimeError(f"Claude declined the request: {msg.stop_details}")
    if msg.stop_reason == "max_tokens":
        raise RuntimeError("Claude's reply was cut off (max_tokens) — no signal this run")
    text = "".join(b.text for b in msg.content if b.type == "text")
    return json.loads(text)


def _http_client() -> Any:
    """On Windows, verify TLS with Python's own ssl module + the Windows
    certificate store. The SDK's default helper (truststore) can hit a
    RecursionError on Windows with recent Python versions."""
    if sys.platform != "win32" or os.environ.get("SSL_CERT_FILE"):
        return None
    import ssl

    from anthropic import DefaultHttpxClient

    return DefaultHttpxClient(verify=ssl.create_default_context())


def _create(client: Any, system: str, user: str, schema: dict[str, Any],
            model: str | None, effort: str | None, max_tokens: int) -> Any:
    return client.beta.messages.create(
        model=model or DEFAULT_MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config={
            "effort": effort or DEFAULT_EFFORT,
            "format": {"type": "json_schema", "schema": schema},
        },
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )


# ── Delivery ─────────────────────────────────────────────────────────────────


def send_email(subject: str, body: str) -> None:
    user = os.environ.get("GMAIL_USER")
    pw = (os.environ.get("GMAIL_APP_PASSWORD") or "").replace(" ", "")
    to = os.environ.get("GMAIL_TO") or user
    if not user or not pw:
        raise RuntimeError("GMAIL_USER / GMAIL_APP_PASSWORD not set. Add them to .env")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.set_content(body)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pw)
        s.send_message(msg)


def send_telegram(text: str) -> bool:
    """Optional. Returns True if delivered, False if skipped or failed."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return False
    # Plain text on purpose: tickers and reasons can contain Markdown
    # characters that make Telegram reject the whole message.
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode("utf-8")
    try:
        http_get(f"https://api.telegram.org/bot{token}/sendMessage", data=data, retries=1)
        return True
    except Exception:
        return False


# ── Logging / entry point ────────────────────────────────────────────────────


def log(scope: str, message: str) -> None:
    """Append-only log per scope (= agent name)."""
    _ensure_dirs()
    line = f"{datetime.now(timezone.utc).isoformat()} [{scope}] {message}\n"
    with (LOGS / f"{scope.lower()}.log").open("a", encoding="utf-8") as f:
        f.write(line)


def run_agent(name: str, main: Callable[[], int]) -> int:
    """Run an agent's main(); log any crash so scheduled runs never fail silently."""
    try:
        return main()
    except Exception as exc:  # noqa: BLE001
        log(name, f"ERROR {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
        print(f"[{name}] ERROR: {exc}", file=sys.stderr)
        return 1


def confidence_steps(value: float, steps: tuple[float, ...]) -> int:
    """1 + number of thresholds `value` meets, capped at 5."""
    return min(5, 1 + sum(1 for s in steps if value >= s))


def render_consensus(ev: ConsensusEvent) -> str:
    """Plain-text body for email + Telegram."""
    head = f"SOPHIE CONSENSUS — {ev.direction} on {ev.ticker}"
    rule = "=" * len(head)
    body = [head, rule, f"Time: {ev.timestamp.isoformat(timespec='minutes')}", ""]
    body.append(f"{len(ev.scouts)} of 5 scouts agree:")
    for reason in ev.reasons:
        body.append(f"  · {reason}")
    body.append("")
    body.append(
        textwrap.fill(
            "This is informational, not a trade instruction. Ross did not "
            "place a trade. The decision is yours.",
            width=72,
        )
    )
    return "\n".join(body)
