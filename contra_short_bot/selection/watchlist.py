"""Daily Watchlist (spec 6.4) and morning prep sheet (spec 6.5), built at 09:25
from premarket data and the daily levels only.

    python -m selection.watchlist --day 2024-03-08          # prints + writes reports/prep/2024-03-08.md
    python -m selection.watchlist --day 2024-03-08 --hits   # ...plus that session's scanner prints
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import time

import numpy as np
import pandas as pd
import yaml

from data.fetch import ROOT, load_config
from levels.library import DayLevels, build_levels
from selection.context import daily_context, has_history
from selection.market_tone import Tone, policy, premarket_tone
from selection.scanners import hits, intraday_scan, premarket_scan, spy_pct_change


@dataclass
class Entry:
    symbol: str
    reasons: list[str]
    rank: float
    ref_price: float            # premarket last, else prior close
    levels: DayLevels
    pm: dict
    focus: bool = False
    focus_why: list[str] = field(default_factory=list)

    def resistance(self, n: int = 2):
        return self.levels.resistance(self.ref_price)[:n]

    def support(self, n: int = 3):
        return self.levels.support(self.ref_price)[:n]


def universe(cfg: dict | None = None) -> list[str]:
    """Symbols the scanners run on. Only the favorites list has data for now
    (universe_mode course_basket / large_cap / sp500 need the wider download)."""
    return yaml.safe_load((ROOT / "watchlist.yaml").read_text())["favorites"]


def favorites() -> set[str]:
    return set(yaml.safe_load((ROOT / "watchlist.yaml").read_text())["favorites"])


def build_watchlist(day, asof: time = time(9, 25), cfg: dict | None = None,
                    symbols: list[str] | None = None) -> tuple[Tone, list[Entry]]:
    cfg = cfg or load_config()
    W = cfg["selection"]["watchlist"]
    day = pd.Timestamp(day).normalize()
    tone = premarket_tone(day, asof, cfg)
    favs = favorites()
    out: list[Entry] = []
    for sym in symbols or universe(cfg):
        if not has_history(sym, day):
            continue
        pm = premarket_scan(sym, day, asof, cfg)
        ctx = daily_context(sym, day)
        ref = pm["pm_last"] if pm["pm_last"] == pm["pm_last"] else ctx.prev_close
        reasons = []
        if pm["bid_up"]:
            reasons.append("gapper_up")
        if pm["ask_down"]:
            reasons.append("gapper_down")
        if sym in favs:
            reasons.append("favorite")
        if not reasons:
            continue
        lv = build_levels(sym, day)
        res = lv.resistance(ref)
        near = [r for r in res if r.zone_low - ref <= cfg["selection"]["level_proximity_atr"] * ctx.atr_d]
        if not near:
            continue  # step 1: needs an active resistance within 2 x ATRd above
        reasons.append("level")
        gap_atr = abs(ref - ctx.prev_close) / ctx.atr_d
        pm_rvol = 0.0 if pm["pm_rvol"] != pm["pm_rvol"] else min(pm["pm_rvol"], 10.0)
        rank = max(r.strength for r in near) * 2 + gap_atr + pm_rvol
        e = Entry(sym, reasons, rank, ref, lv, pm)
        # 2b: morning short bias ("focus stocks").
        if pm["ask_down"]:
            e.focus_why.append("pm down 3.5%")
        if ref < ctx.prev_close * (1 - cfg["selection"]["gap_static_pct"]) and ref < ctx.prev_low:
            e.focus_why.append("gapping below yesterday's low")
        if ctx.trendsell_setup():
            e.focus_why.append("Trendsell setup")
        if near[0].zone_low - ref <= W["focus_resistance_atr"] * ctx.atr_d:
            e.focus_why.append("key resistance within 1 ATRd")
        out.append(e)
    out.sort(key=lambda e: -e.rank)
    out = out[:W["size"]]
    # The top focus_count candidates with a short-bias reason become focus stocks.
    for e in [e for e in out if e.focus_why][:W["focus_count"]]:
        e.focus = True
        e.rank += W["focus_bonus"]
    out.sort(key=lambda e: (not e.focus, -e.rank))
    return tone, out


def _armed(e: Entry, tone: Tone, cfg: dict) -> str:
    """The generated 'Additional Notes' line (spec 6.5)."""
    pol = policy(tone.label, cfg)
    parts = []
    res = e.resistance(1)
    if res and pol["fade"]:
        r = res[0]
        kinds = "/".join(r.labels[:2])
        need = " on a volume climax" if pol["fade_needs_climax"] else " if spike on volume"
        letter = "C" if r.kind == "gap_fill" else "B" if r.kind == "trendline" else "A"
        parts.append(f"{letter} ({kinds} {r.mid:.2f}){need}")
    sup = e.support(1)
    if sup and pol["trend"] and (e.focus or "gapper_down" in e.reasons):
        s = sup[0]
        parts.append(f"D if break below {s.mid:.2f} ({'/'.join(s.labels[:2])})")
    note = ("Armed: " + "; ".join(parts)) if parts else "Nothing armed"
    if e.focus:
        note += f". Focus: {', '.join(e.focus_why)}"
    return note


def prep_sheet(day, tone: Tone, entries: list[Entry], cfg: dict | None = None) -> str:
    cfg = cfg or load_config()
    day = pd.Timestamp(day)
    pol = policy(tone.label, cfg)
    lines = [f"# Prep sheet {day:%Y-%m-%d}", "",
             f"**Market tone (09:25):** {tone}. Fades {'allowed' if pol['fade'] else 'off'}"
             f"{' (top ' + str(100 - pol['fade_min_strength_pct']) + '% levels only)' if pol['fade_min_strength_pct'] else ''}"
             f"{', volume climax required' if pol['fade_needs_climax'] else ''}; "
             f"trend continuation {'allowed' if pol['trend'] else 'off'}"
             f"{' (relative weakness required)' if pol['trend_needs_rel_weak'] else ''}; size {pol['size']:.0%}.", ""]
    tone_rows = [f"{s} {p['score']:+.1f} (gap {p['gap_atr']:+.2f} ATR)" for s, p in tone.parts.items() if p]
    lines += ["Index proxies: " + ", ".join(tone_rows), "",
              "| Stock | Premkt | Key Support | Key Resistance | Additional Notes |", "|---|---|---|---|---|"]
    for e in entries:
        pm = e.pm
        pmtxt = f"{e.ref_price:.2f} ({pm['gap_pct']:+.1%})" if pm["pm_last"] == pm["pm_last"] else "-"
        name = f"**{e.symbol}**" if e.focus else e.symbol
        lines.append(f"| {name} | {pmtxt} | {', '.join(l.label() for l in e.support())} | "
                     f"{', '.join(l.label() for l in e.resistance())} | {_armed(e, tone, cfg)}. "
                     f"Why listed: {', '.join(e.reasons)} |")
    lines += ["", "Bold = focus stock (morning short bias)."]
    return "\n".join(lines) + "\n"


def session_hits(day, entries: list[Entry], cfg: dict) -> pd.DataFrame:
    """Every intraday scan print for the watchlist symbols, with whether a Contra
    Short print came within level_atr5 x ATR5 below an active resistance (the
    A/B/C candidate rule, spec 6.2b)."""
    spy = spy_pct_change(day)
    rows = []
    for e in entries:
        df = intraday_scan(e.symbol, day, cfg, spy_pct=spy)
        if df.empty:
            continue
        h = hits(df, e.symbol)
        if h.empty:
            continue
        near = []
        for t, scan, px in zip(h["time"], h["scan"], h["price"]):
            lv = ""
            if scan == "contra_short":
                a5 = df.at[t, "atr5"]
                above = [l for l in e.levels.resistance(px) if l.zone_low - px <= cfg["selection"]["contra"]["level_atr5"] * a5]
                lv = above[0].label() if above else ""
            near.append(lv)
        h["level"] = near
        rows.append(h)
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", required=True)
    ap.add_argument("--hits", action="store_true", help="also run the intraday scanners and list their prints")
    args = ap.parse_args()
    cfg = load_config()
    tone, entries = build_watchlist(args.day, cfg=cfg)
    md = prep_sheet(args.day, tone, entries, cfg)
    if args.hits:
        h = session_hits(args.day, entries, cfg)
        if len(h):
            cs = h[h["scan"] == "contra_short"]
            first = cs.sort_values("time").groupby(["symbol", "tier"]).head(1)
            md += "\n## Contra Short prints (first print per stock and tier)\n\n| Time | Stock | Tier | Price | Vdif | RVOL | Level within 1 x ATR5 |\n|---|---|---|---|---|---|---|\n"
            md += "".join(f"| {r.time:%H:%M} | {r.symbol} | {r.tier} | {r.price} | {r.vdif} | {r.rvol} | {r.level or '-'} |\n"
                          for r in first.itertuples())
            other = h[h["scan"] != "contra_short"].groupby("scan")["symbol"].apply(lambda s: ", ".join(sorted(set(s))))
            md += "\n## Other scan prints\n\n" + "".join(f"- **{k}**: {v}\n" for k, v in other.items())
            h.to_csv(ROOT / "reports" / "prep" / f"{pd.Timestamp(args.day):%Y-%m-%d}_hits.csv", index=False)
    out = ROOT / "reports" / "prep" / f"{pd.Timestamp(args.day):%Y-%m-%d}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md)
    print(md)
    print(out)


if __name__ == "__main__":
    main()
