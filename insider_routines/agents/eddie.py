#!/usr/bin/env python3
"""
Eddie — the SEC Form 4 watcher.

Downloads SEC EDGAR's daily index of every filing, pulls each Form 4
(insider transaction report), and keeps open-market purchases
(transaction code P) worth >= $100,000 by a CEO / CFO / President /
Chair / Director. Emits one BULLISH signal per ticker, strongest first.

Schedule: daily, 06:00 local time (EDGAR publishes the previous business
day's index each evening). Each index day is processed once.
"""

from __future__ import annotations

import os
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (
    BULLISH,
    HTTPNotFound,
    Signal,
    confidence_steps,
    http_get,
    kv_get,
    kv_set,
    log,
    record_signals,
    run_agent,
    sec_headers,
)

MIN_VALUE = 100_000
MAX_SIGNALS = int(os.environ.get("EDDIE_MAX_SIGNALS", "10"))
MAX_DAYS_PER_RUN = 3  # catch up after the computer was off, but not forever
LOOKBACK_DAYS = 6  # how far back to look for the latest published index

TOP_ROLE = re.compile(r"\b(CEO|CHIEF EXECUTIVE|CHAIR)")
MID_ROLE = re.compile(r"\b(CFO|CHIEF FINANCIAL|PRESIDENT)")


@dataclass
class Buy:
    ticker: str
    issuer: str
    insider: str
    role: str
    top_exec: bool
    value: float
    filing: str
    trade: tuple = ()  # (date, shares, price) — identifies the same trade across filings


def index_url(d: date) -> str:
    q = (d.month - 1) // 3 + 1
    return (
        f"https://www.sec.gov/Archives/edgar/daily-index/{d.year}/QTR{q}/"
        f"form.{d:%Y%m%d}.idx"
    )


def parse_form_index(text: str) -> list[str]:
    """Return the unique archive paths of all Form 4 filings in a form.idx file."""
    paths: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0] == "4" and parts[-1].endswith(".txt"):
            if parts[-1] not in seen:
                seen.add(parts[-1])
                paths.append(parts[-1])
    return paths


def _flag(v: str | None) -> bool:
    return (v or "").strip().lower() in ("1", "true")


def _num(v: str | None) -> float:
    try:
        return float((v or "0").replace(",", ""))
    except ValueError:
        return 0.0


def parse_form4(doc: str, filing: str = "") -> list[Buy]:
    """Extract qualifying open-market buys from one Form 4 submission."""
    m = re.search(r"<ownershipDocument>.*?</ownershipDocument>", doc, re.S)
    if not m:
        return []
    try:
        root = ET.fromstring(m.group(0))
    except ET.ParseError:
        return []

    ticker = (root.findtext("issuer/issuerTradingSymbol") or "").strip().upper()
    if not ticker or ticker in ("NONE", "N/A", "NA"):
        return []
    issuer = (root.findtext("issuer/issuerName") or "").strip()

    # Joint filings list several owners; credit the most senior qualifying one.
    best: tuple[int, str, str] | None = None  # (rank, name, role)
    for ro in root.findall("reportingOwner"):
        name = (ro.findtext("reportingOwnerId/rptOwnerName") or "").strip()
        rel = ro.find("reportingOwnerRelationship")
        if rel is None:
            continue
        title = f"{rel.findtext('officerTitle') or ''} {rel.findtext('otherText') or ''}".strip()
        upper = title.upper()
        if TOP_ROLE.search(upper):
            rank = 3
        elif MID_ROLE.search(upper):
            rank = 2
        elif _flag(rel.findtext("isDirector")):
            rank, title = 1, title or "Director"
        else:
            continue
        if best is None or rank > best[0]:
            best = (rank, name, title)
    if best is None:
        return []

    rank, name, role = best
    buys = []
    for tx in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        code = (tx.findtext("transactionCoding/transactionCode") or "").strip()
        acq = (tx.findtext("transactionAmounts/transactionAcquiredDisposedCode/value") or "").strip()
        if code != "P" or acq not in ("A", ""):
            continue
        shares = _num(tx.findtext("transactionAmounts/transactionShares/value"))
        price = _num(tx.findtext("transactionAmounts/transactionPricePerShare/value"))
        when = (tx.findtext("transactionDate/value") or "").strip()
        if shares * price > 0:
            buys.append(Buy(ticker, issuer, name, role, rank == 3, shares * price, filing, (when, shares, price)))
    return buys


def build_signals(buys: list[Buy], day: str) -> list[Signal]:
    """Group buys by insider, apply the $100k floor, then roll up per ticker.

    The same trade is often reported in several filings (e.g. a director and
    the company they control each file one), so identical trades — same
    insider, date, share count and price — are counted once.
    """
    per_insider: dict[tuple[str, str], Buy] = {}
    seen: set[tuple] = set()
    for b in buys:
        if b.trade:
            trade_key = (b.ticker, b.insider, b.trade)
            if trade_key in seen:
                continue
            seen.add(trade_key)
        key = (b.ticker, b.insider)
        if key in per_insider:
            per_insider[key].value += b.value
        else:
            per_insider[key] = Buy(**b.__dict__)

    per_ticker: dict[str, list[Buy]] = {}
    for b in per_insider.values():
        if b.value >= MIN_VALUE:
            per_ticker.setdefault(b.ticker, []).append(b)

    sigs: list[tuple[float, Signal]] = []
    for ticker, group in per_ticker.items():
        group.sort(key=lambda b: b.value, reverse=True)
        total = sum(b.value for b in group)
        lead = group[0]
        conf = confidence_steps(total, (250_000, 500_000, 1_000_000))
        if any(b.top_exec and b.value >= 1_000_000 for b in group):
            conf = 5
        if len(group) >= 2:
            conf = min(5, conf + 1)
        who = f"{lead.insider} ({lead.role})"
        if len(group) > 1:
            who += f" + {len(group) - 1} other insider(s)"
        reason = f"{who} bought ${total:,.0f} of {ticker} ({lead.issuer}) on the open market, filed {day}"
        raw = "\n".join(
            f"{b.insider} | {b.role} | ${b.value:,.0f} | https://www.sec.gov/Archives/{b.filing}"
            for b in group
        )
        sigs.append((total, Signal("eddie", ticker, BULLISH, conf, reason[:240], raw)))
    sigs.sort(key=lambda t: t[0], reverse=True)
    return [s for _, s in sigs[:MAX_SIGNALS]]


def main() -> int:
    headers = sec_headers()
    last = kv_get("eddie.last_index_date", "")

    # Find published daily indexes newer than the last one processed.
    todo: list[tuple[date, str]] = []
    today = date.today()
    for back in range(LOOKBACK_DAYS + 1):
        d = today - timedelta(days=back)
        if d.weekday() >= 5 or d.isoformat() <= (last or ""):
            continue
        try:
            # SEC answers 403 (not 404) for an index it hasn't published yet.
            text = http_get(index_url(d), headers=headers, not_found=(403, 404)).decode("latin-1")
        except HTTPNotFound:
            continue  # not published yet, or a holiday
        todo.append((d, text))
        if not last or len(todo) >= MAX_DAYS_PER_RUN:
            break  # first run: just the latest day
    if not todo and not last:
        raise RuntimeError(
            f"no EDGAR daily index found for the last {LOOKBACK_DAYS} days — SEC may be "
            "blocking you; check SEC_USER_AGENT in .env and any VPN"
        )
    if not todo:
        print("[eddie] no new EDGAR daily index since last run")
        log("eddie", "no new daily index")
        return 0

    for d, text in sorted(todo):
        paths = parse_form_index(text)
        log("eddie", f"{d}: scanning {len(paths)} Form 4 filings")
        print(f"[eddie] {d}: scanning {len(paths)} Form 4 filings (takes a few minutes)…")
        buys: list[Buy] = []
        for p in paths:
            try:
                doc = http_get(f"https://www.sec.gov/Archives/{p}", headers=headers)
            except HTTPNotFound:
                continue
            buys.extend(parse_form4(doc.decode("utf-8", "replace"), p))
        sigs = build_signals(buys, d.isoformat())
        if sigs:
            record_signals("eddie", sigs)
        else:
            print(f"[eddie] {d}: no qualifying insider buys")
            log("eddie", f"{d}: no qualifying insider buys")
        kv_set("eddie.last_index_date", d.isoformat())
    return 0


if __name__ == "__main__":
    raise SystemExit(run_agent("eddie", main))
