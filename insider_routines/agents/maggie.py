#!/usr/bin/env python3
"""
Maggie — the smart-money tracker.

For each watched fund, pulls the two most recent 13F-HR filings from
SEC EDGAR, diffs the holdings, and surfaces moves worth >= $50M:
  NEW POSITION / INCREASED (>= 25% more shares) → BULLISH
  EXITED                                        → BEARISH
13F filings list CUSIPs, not tickers; Maggie maps them with OpenFIGI's
free API (falling back to SEC's company-name → ticker list).

Schedule: weekly, Sunday 19:00 local time. A fund's filing is only
reported once, and only if it was filed in the last FRESH_DAYS days
(13Fs land ~45 days after each quarter: mid-Feb, May, Aug, Nov).
Run with --force to report each fund's latest filing regardless.
"""

from __future__ import annotations

import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (
    BEARISH,
    BULLISH,
    FORCE,
    Signal,
    confidence_steps,
    http_get,
    http_json,
    kv_get,
    kv_set,
    log,
    record_signals,
    run_agent,
    sec_headers,
)

FUNDS = [
    ("Berkshire Hathaway", "0001067983"),
    ("Bridgewater Associates", "0001350694"),
    ("Renaissance Technologies", "0001037389"),
    ("Citadel Advisors", "0001423053"),
    ("Two Sigma Investments", "0001179392"),
]

# Broad index / sector / bond ETFs. Big funds hold these mostly as hedges,
# so changes in them say little about conviction. Ignored.
INDEX_ETFS = {
    "SPY", "IVV", "VOO", "SPLG", "RSP", "VTI", "ITOT", "QQQ", "QQQM", "IWM",
    "IWB", "IWF", "IWD", "IWR", "IJH", "IJR", "MDY", "DIA", "VUG", "VTV",
    "EFA", "IEFA", "VEA", "EEM", "IEMG", "VWO", "ACWI", "VT", "EWJ", "FXI",
    "AGG", "BND", "TLT", "IEF", "SHY", "LQD", "HYG", "JNK", "TIP", "GOVT",
    "GLD", "IAU", "SLV", "USO", "SMH", "SOXX", "KRE", "XBI", "GDX",
    "XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLU", "XLB", "XLRE", "XLC",
}

MIN_MOVE_USD = 50_000_000
INCREASE_PCT = 0.25
FRESH_DAYS = 14
MAX_SIGNALS = int(os.environ.get("MAGGIE_MAX_SIGNALS", "10"))


# ── Parsing / diffing (pure functions) ───────────────────────────────────────


def latest_13f(submissions: dict, n: int = 2) -> list[dict]:
    """The n most recent original 13F-HR filings from an EDGAR submissions JSON."""
    recent = submissions.get("filings", {}).get("recent", {})
    out = []
    for i, form in enumerate(recent.get("form", [])):
        if form == "13F-HR":
            out.append(
                {
                    "accession": recent["accessionNumber"][i],
                    "filed": recent["filingDate"][i],
                    "period": (recent.get("reportDate") or [""] * (i + 1))[i],
                }
            )
            if len(out) == n:
                break
    return out


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_infotable(xml_text: str) -> dict[str, dict]:
    """CUSIP → {name, value (USD), shares}. Option rows (put/call) are skipped."""
    root = ET.fromstring(xml_text)
    out: dict[str, dict] = {}
    for row in root.iter():
        if _local(row.tag) != "infoTable":
            continue
        fields: dict[str, str] = {}
        for el in row.iter():
            if el.text and el.text.strip():
                fields[_local(el.tag)] = el.text.strip()
        if fields.get("putCall"):
            continue
        cusip = fields.get("cusip", "").upper()
        if not cusip:
            continue
        h = out.setdefault(cusip, {"name": fields.get("nameOfIssuer", ""), "value": 0.0, "shares": 0.0})
        h["value"] += float(fields.get("value", "0") or 0)
        h["shares"] += float(fields.get("sshPrnamt", "0") or 0)
    return out


def diff_holdings(prev: dict[str, dict], cur: dict[str, dict]) -> list[dict]:
    """Moves worth >= MIN_MOVE_USD between two quarters' holdings."""
    moves = []
    for cusip, h in cur.items():
        p = prev.get(cusip)
        if p is None:
            if h["value"] >= MIN_MOVE_USD:
                moves.append({"kind": "NEW POSITION", "cusip": cusip, "name": h["name"], "usd": h["value"]})
        elif p["shares"] > 0 and h["shares"] >= p["shares"] * (1 + INCREASE_PCT):
            price = h["value"] / h["shares"] if h["shares"] else 0
            added = (h["shares"] - p["shares"]) * price
            if added >= MIN_MOVE_USD:
                pct = 100 * (h["shares"] / p["shares"] - 1)
                moves.append({"kind": f"INCREASED +{pct:.0f}%", "cusip": cusip, "name": h["name"], "usd": added})
    for cusip, p in prev.items():
        if cusip not in cur and p["value"] >= MIN_MOVE_USD:
            moves.append({"kind": "EXITED", "cusip": cusip, "name": p["name"], "usd": p["value"]})
    return moves


_SUFFIXES = {
    "INC", "CORP", "CORPORATION", "CO", "COMPANY", "LTD", "PLC", "THE", "COM",
    "CL", "CLASS", "A", "B", "C", "NEW", "DEL", "SA", "NV", "AG", "SHS", "ORD",
}


def norm_name(name: str) -> str:
    words = re.sub(r"[^A-Z0-9 ]", " ", name.upper()).split()
    return " ".join(w for w in words if w not in _SUFFIXES)


def build_signals(moves: list[dict]) -> list[Signal]:
    """Roll moves up per (ticker, direction) across funds. Moves need 'ticker' + 'fund'."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for m in moves:
        direction = BEARISH if m["kind"] == "EXITED" else BULLISH
        groups.setdefault((m["ticker"], direction), []).append(m)

    sigs: list[tuple[float, Signal]] = []
    for (ticker, direction), group in groups.items():
        if ticker in INDEX_ETFS:
            continue
        group.sort(key=lambda m: m["usd"], reverse=True)
        total = sum(m["usd"] for m in group)
        funds = sorted({m["fund"] for m in group})
        conf = confidence_steps(total, (250_000_000, 1_000_000_000))
        conf += any(m["kind"] == "NEW POSITION" for m in group)
        conf += len(funds) >= 2
        lead = group[0]
        reason = f"{lead['fund']}: {lead['kind']} in {ticker} (${lead['usd'] / 1e6:,.0f}M, 13F filed {lead['filed']})"
        if len(funds) > 1:
            reason += f" + {len(funds) - 1} other fund(s) same direction"
        raw = json.dumps(group, indent=1)
        sigs.append((total, Signal("maggie", ticker, direction, min(5, conf), reason[:240], raw)))
    sigs.sort(key=lambda t: t[0], reverse=True)
    return [s for _, s in sigs[:MAX_SIGNALS]]


# ── Network ──────────────────────────────────────────────────────────────────


def fetch_holdings(cik: str, accession: str, headers: dict) -> dict[str, dict]:
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}"
    items = http_json(f"{base}/index.json", headers=headers)["directory"]["item"]
    xmls = [i["name"] for i in items if i["name"].lower().endswith(".xml") and i["name"].lower() != "primary_doc.xml"]
    if not xmls:
        raise RuntimeError(f"no information table in {accession}")
    xmls.sort(key=lambda n: "info" not in n.lower())  # prefer *infotable*.xml
    return parse_infotable(http_get(f"{base}/{xmls[0]}", headers=headers).decode("utf-8", "replace"))


def map_tickers(cusips: list[str], names: dict[str, str], headers: dict) -> dict[str, str]:
    """CUSIP → ticker. Cached in the kv table; OpenFIGI first, SEC name list second."""
    out: dict[str, str] = {}
    todo = []
    for c in cusips:
        cached = kv_get(f"figi:{c}")
        if cached:
            out[c] = cached
        else:
            todo.append(c)

    key = os.environ.get("OPENFIGI_API_KEY")
    batch = 100 if key else 10
    fig_headers = {"Content-Type": "application/json"}
    if key:
        fig_headers["X-OPENFIGI-APIKEY"] = key
    for i in range(0, len(todo), batch):
        chunk = todo[i : i + batch]
        jobs = [{"idType": "ID_CUSIP", "idValue": c, "exchCode": "US"} for c in chunk]
        try:
            res = json.loads(
                http_get("https://api.openfigi.com/v3/mapping", headers=fig_headers, data=json.dumps(jobs).encode())
            )
        except Exception as exc:  # noqa: BLE001
            log("maggie", f"OpenFIGI lookup failed: {exc}")
            break
        for c, r in zip(chunk, res):
            data = r.get("data") or []
            if data and data[0].get("ticker"):
                out[c] = data[0]["ticker"].upper().replace("/", ".")
                kv_set(f"figi:{c}", out[c])

    missing = [c for c in cusips if c not in out]
    if missing:
        try:
            listing = http_json("https://www.sec.gov/files/company_tickers.json", headers=headers)
            by_name = {norm_name(v["title"]): v["ticker"].upper() for v in listing.values()}
            for c in missing:
                t = by_name.get(norm_name(names.get(c, "")))
                if t:
                    out[c] = t
        except Exception as exc:  # noqa: BLE001
            log("maggie", f"SEC ticker list failed: {exc}")
    return out


def main() -> int:
    headers = sec_headers()
    cutoff = (date.today() - timedelta(days=FRESH_DAYS)).isoformat()
    moves: list[dict] = []
    for fund, cik in FUNDS:
        subs = http_json(f"https://data.sec.gov/submissions/CIK{cik}.json", headers=headers)
        filings = latest_13f(subs)
        if len(filings) < 2:
            log("maggie", f"{fund}: fewer than two 13F-HR filings found, skipping")
            continue
        cur, prev = filings
        seen = kv_get(f"maggie.{cik}")
        if not FORCE and (cur["accession"] == seen or cur["filed"] < cutoff):
            log("maggie", f"{fund}: no new 13F (latest filed {cur['filed']})")
            continue
        print(f"[maggie] {fund}: diffing 13F filed {cur['filed']} vs {prev['filed']}")
        diff = diff_holdings(
            fetch_holdings(cik, prev["accession"], headers),
            fetch_holdings(cik, cur["accession"], headers),
        )
        for m in diff:
            m.update(fund=fund, filed=cur["filed"], accession=cur["accession"])
        moves.extend(diff)
        kv_set(f"maggie.{cik}", cur["accession"])

    if not moves:
        print("[maggie] no new 13F moves ≥ $50M this week")
        log("maggie", "no new 13F moves")
        return 0

    tickers = map_tickers(sorted({m["cusip"] for m in moves}), {m["cusip"]: m["name"] for m in moves}, headers)
    mapped = []
    for m in moves:
        if m["cusip"] in tickers:
            mapped.append({**m, "ticker": tickers[m["cusip"]]})
        else:
            log("maggie", f"no ticker for CUSIP {m['cusip']} ({m['name']}) — skipped")
    record_signals("maggie", build_signals(mapped))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_agent("maggie", main))
