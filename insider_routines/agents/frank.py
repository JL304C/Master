#!/usr/bin/env python3
"""
Frank — the Fed-speech reader.

Pulls the Federal Reserve's official RSS feeds (speeches, testimony,
monetary-policy press releases), downloads every item from the last 7
days, and has Claude classify each one hawkish / dovish / neutral from
the actual text. Each classification must quote the speech; quotes that
don't appear in the text are thrown out. Frank tallies the rest in code
and emits one MACRO signal.

Schedule: weekly, Monday 08:00 local time.
"""

from __future__ import annotations

import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (
    BEARISH,
    BULLISH,
    NEUTRAL,
    Signal,
    ask_claude_json,
    http_get,
    log,
    record_signals,
    run_agent,
)

FEEDS = [
    "https://www.federalreserve.gov/feeds/speeches.xml",
    "https://www.federalreserve.gov/feeds/testimony.xml",
    "https://www.federalreserve.gov/feeds/press_monetary.xml",
]
WINDOW_DAYS = 7
MAX_CHARS = 60_000  # per document; Fed speeches are almost always far shorter
HEADERS = {"User-Agent": "InsiderRoutines/1.0 (Fed speech reader)"}

SYSTEM = """You are Frank, a macro analyst. You will be given the full text
of Federal Reserve speeches, testimony and FOMC statements. Classify each
document's monetary-policy stance using ONLY the text provided:

  HAWKISH — favors tighter policy: holding rates higher for longer, hikes,
            inflation concerns dominating.
  DOVISH  — favors easier policy: cuts, labor-market or growth concerns
            dominating.
  NEUTRAL — balanced, data-dependent, or not about policy at all.

For every document return its id, speaker, stance, a one-sentence summary,
and one supporting quote of at most 25 words copied EXACTLY, character for
character, from that document's text. If no sentence supports a stance,
use NEUTRAL and quote the most relevant sentence."""

SCHEMA = {
    "type": "object",
    "properties": {
        "documents": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "speaker": {"type": "string"},
                    "stance": {"type": "string", "enum": ["HAWKISH", "DOVISH", "NEUTRAL"]},
                    "summary": {"type": "string"},
                    "quote": {"type": "string"},
                },
                "required": ["id", "speaker", "stance", "summary", "quote"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["documents"],
    "additionalProperties": False,
}


def parse_feed(xml_bytes: bytes, since: datetime) -> list[dict]:
    """RSS items published at or after `since`: [{title, link, published}]."""
    root = ET.fromstring(xml_bytes)
    items = []
    for it in root.iter("item"):
        link = (it.findtext("link") or "").strip()
        pub = it.findtext("pubDate")
        if not link or not pub:
            continue
        try:
            when = parsedate_to_datetime(pub)
        except (TypeError, ValueError):
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when >= since:
            items.append({"title": (it.findtext("title") or "").strip(), "link": link, "published": when.isoformat()})
    return items


class _ArticleText(HTMLParser):
    """Collect text inside <div id="article"> (Fed page body); everything as fallback."""

    VOID = {"br", "img", "hr", "meta", "link", "input", "source", "wbr", "area", "base", "col"}
    BLOCK = {"p", "div", "br", "li", "tr", "td", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"}

    def __init__(self) -> None:
        super().__init__()
        self.depth = 0
        self.skip = 0
        self.article: list[str] = []
        self.all: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        if tag in self.BLOCK:
            self.handle_data(" ")
        if tag in self.VOID:
            return
        if self.depth:
            self.depth += 1
        elif dict(attrs).get("id") == "article":
            self.depth = 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1
        if tag in self.BLOCK:
            self.handle_data(" ")
        if self.depth and tag not in self.VOID:
            self.depth -= 1

    def handle_data(self, data):
        if self.skip:
            return
        self.all.append(data)
        if self.depth:
            self.article.append(data)


def page_text(html: str) -> str:
    p = _ArticleText()
    p.feed(html)
    text = "".join(p.article) or "".join(p.all)
    return re.sub(r"\s+", " ", text).strip()


def _squash(s: str) -> str:
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = s.replace("—", "-").replace("–", "-")
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def quote_found(quote: str, text: str) -> bool:
    q = _squash(quote)
    return len(q) >= 12 and q in _squash(text)


def tally(results: list[dict], docs: list[dict]) -> tuple[Signal | None, list[dict]]:
    """Keep verified classifications, compute net tilt. Returns (signal, kept)."""
    by_id = {d["id"]: d for d in docs}
    kept = []
    for r in results:
        doc = by_id.get(r.get("id"))
        if doc is None:
            continue
        if not quote_found(r.get("quote", ""), doc["text"]):
            log("frank", f"dropped unverified quote for {doc['link']}: {r.get('quote', '')!r}")
            continue
        kept.append({**r, "link": doc["link"], "title": doc["title"]})
    if not kept:
        return None, kept
    hawk = sum(r["stance"] == "HAWKISH" for r in kept)
    dove = sum(r["stance"] == "DOVISH" for r in kept)
    neutral = len(kept) - hawk - dove
    net = hawk - dove
    # For risk assets: hawkish = BEARISH, dovish = BULLISH.
    direction = BEARISH if net > 0 else BULLISH if net < 0 else NEUTRAL
    tilt = "hawkish" if net > 0 else "dovish" if net < 0 else "mixed"
    reason = f"Fed last {WINDOW_DAYS}d: {hawk} hawkish, {dove} dovish, {neutral} neutral → net {tilt}"
    raw = json.dumps(kept, indent=1)
    return Signal("frank", "MACRO", direction, min(5, 1 + abs(net)), reason, raw), kept


def main() -> int:
    since = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)
    items: dict[str, dict] = {}
    for feed in FEEDS:
        for it in parse_feed(http_get(feed, headers=HEADERS), since):
            items.setdefault(it["link"], it)
    if not items:
        print(f"[frank] no Fed speeches or statements in the last {WINDOW_DAYS} days")
        log("frank", "no Fed items in window")
        return 0

    docs = []
    for i, it in enumerate(items.values(), 1):
        text = page_text(http_get(it["link"], headers=HEADERS).decode("utf-8", "replace"))
        if len(text) > MAX_CHARS:
            log("frank", f"{it['link']} is {len(text)} chars; sending the first {MAX_CHARS}")
            text = text[:MAX_CHARS]
        docs.append({**it, "id": i, "text": text})
    print(f"[frank] classifying {len(docs)} Fed document(s) with Claude…")

    body = "\n\n".join(
        f'<document id="{d["id"]}" title="{d["title"]}" published="{d["published"]}">\n{d["text"]}\n</document>'
        for d in docs
    )
    result = ask_claude_json(SYSTEM, body, SCHEMA)
    sig, kept = tally(result.get("documents", []), docs)
    for r in kept:
        print(f"[frank]   {r['stance']:<8} {r['speaker']}: {r['summary']}")
    if sig is None:
        print("[frank] no classification could be verified against the source text")
        log("frank", "no verified classifications")
        return 0
    record_signals("frank", [sig])
    return 0


if __name__ == "__main__":
    raise SystemExit(run_agent("frank", main))
