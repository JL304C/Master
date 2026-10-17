"""Backtest report (spec 11) as one self-contained HTML page.

    python -m backtest.report --name base            # -> reports/backtest/base/report.html
    python -m backtest.report --name base --no-charts

Headline metrics are shown for the in-sample and out-of-sample periods side by
side. Every breakdown table is computed on the IN-SAMPLE trades only, so the
out-of-sample period is never used to choose settings (spec 10).
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import re

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from data.bars import sessions
from data.fetch import ROOT, load_config

STRATS = ["A", "B", "C", "D", "A0"]
# Validated categorical slots (dataviz reference palette, light), fixed order per strategy.
COLOR = {"A": "#2a78d6", "B": "#eb6834", "C": "#1baf7a", "D": "#eda100", "A0": "#e87ba4", "All": "#0b0b0b"}
INK, INK2, GRID = "#0b0b0b", "#52514e", "#e4e3df"


# ------------------------------------------------------------------ metrics

def metrics(t: pd.DataFrame, days: list, equity: float) -> dict:
    if t.empty:
        return {"trades": 0}
    pnl = t["pnl"]
    gw, gl = pnl[pnl > 0].sum(), -pnl[pnl < 0].sum()
    daily = t.groupby("date")["pnl"].sum().reindex([f"{d:%Y-%m-%d}" for d in days], fill_value=0.0)
    curve = daily.cumsum()
    dd = (curve - curve.cummax()).min()
    years = max(len(days) / 252, 1e-9)
    streak = run = 0
    for x in t.sort_values("entry_time")["pnl"]:
        run = run + 1 if x < 0 else 0
        streak = max(streak, run)
    return {"trades": len(t), "win %": round((pnl > 0).mean() * 100, 1),
            "avg R": round(t["R"].mean(), 3), "expectancy R": round(t["R"].mean(), 3),
            "profit factor": round(gw / gl, 2) if gl > 0 else np.inf,
            "net P&L $": round(pnl.sum()), "CAGR %": round(((1 + pnl.sum() / equity) ** (1 / years) - 1) * 100, 2),
            "max DD $": round(dd), "max DD %": round(dd / equity * 100, 2),
            "Sharpe": round(daily.mean() / daily.std() * np.sqrt(252), 2) if daily.std() > 0 else np.nan,
            "longest losing streak": streak}


def breakdown(t: pd.DataFrame, by, min_n: int = 1) -> pd.DataFrame:
    if t.empty:
        return pd.DataFrame()
    g = t.groupby(by, dropna=False)
    out = pd.DataFrame({"trades": g.size(), "win %": g["pnl"].apply(lambda x: (x > 0).mean() * 100).round(1),
                        "avg R": g["R"].mean().round(3), "net P&L $": g["pnl"].sum().round(0)})
    return out[out["trades"] >= min_n]


# ------------------------------------------------------------------ charts

def _png(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, facecolor="#fcfcfb")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def _style(ax):
    ax.set_facecolor("#fcfcfb")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.tick_params(colors=INK2, labelsize=8)
    ax.grid(axis="y", color=GRID, linewidth=0.6)


def equity_chart(t: pd.DataFrame, days: list, split: pd.Timestamp) -> str:
    idx = pd.DatetimeIndex(days)
    fig, ax = plt.subplots(figsize=(11, 4.6))
    _style(ax)
    series = [("All", t)] + [(s, t[t["strategy"] == s]) for s in STRATS if (t["strategy"] == s).any()]
    for name, sub in series:
        c = sub.groupby(pd.to_datetime(sub["date"]))["pnl"].sum().reindex(idx, fill_value=0).cumsum() / 1000
        ax.plot(idx, c, color=COLOR[name], linewidth=2 if name == "All" else 1.6)
        ax.text(idx[-1], c.iloc[-1], f"  {name} {c.iloc[-1]:+,.0f}k", color=INK, fontsize=8, va="center")
    ax.axvline(split, color=INK2, linewidth=0.8, linestyle=":")
    ax.text(split, ax.get_ylim()[1], " out-of-sample →", color=INK2, fontsize=8, va="top")
    ax.axhline(0, color=INK2, linewidth=0.6)
    ax.set_ylabel("cumulative P&L, $ thousands (fixed $1M sizing)", color=INK2, fontsize=8)
    ax.set_xlim(idx[0], idx[-1] + (idx[-1] - idx[0]) * 0.12)
    fig.tight_layout()
    return _png(fig)


def r_hist(t: pd.DataFrame) -> str:
    fig, ax = plt.subplots(figsize=(11, 3.2))
    _style(ax)
    r = t["R"].clip(-3, 8).dropna()
    ax.hist(r, bins=np.arange(-3, 8.25, 0.25), color=COLOR["A"], edgecolor="#fcfcfb", linewidth=1)
    ax.axvline(0, color=INK2, linewidth=0.8)
    ax.set_xlabel("R per trade (clipped to -3 .. 8)", color=INK2, fontsize=8)
    ax.set_ylabel("trades", color=INK2, fontsize=8)
    fig.tight_layout()
    return _png(fig)


def trade_charts(rows: pd.DataFrame, cfg: dict) -> list[tuple[str, str, str]]:
    """1-minute and 5-minute charts for each trade: zone, entry, first stop,
    targets, exits, VWAP and EMA20. Re-runs each trade's day to get its bars."""
    from backtest.runner import run_day
    from data.bars import resample
    from tools.level_charts import _candles
    out = []
    by_day: dict = {}
    for r in rows.itertuples():
        by_day.setdefault(r.date, []).append(r)
    for day, rs in by_day.items():
        _, log, traders = run_day(day, cfg, symbols=sorted({r.symbol for r in rs}), keep_traders=True)
        for r in rs:
            tr = traders.get(r.symbol)
            if tr is None:
                continue
            f = tr.f
            et, xt = pd.Timestamp(r.entry_time), pd.Timestamp(r.exit_time)
            tg = [l for l in log if f" {r.symbol} targets:" in l and l.startswith(f"{et:%H:%M}")]
            tgt = [float(x) for x in re.findall(r"T\d ([\d.]+)", tg[0])] if tg else []
            m = re.search(r"of (.+?) (\d+\.\d+)-(\d+\.\d+)", str(r.reason))
            imgs = []
            for mins, pad in ((1, 40), (5, 150)):
                v = f[(f.index >= et - pd.Timedelta(minutes=pad)) & (f.index <= xt + pd.Timedelta(minutes=pad // 2))]
                b = v if mins == 1 else resample(v, 5)
                fig, ax = plt.subplots(figsize=(6.4, 3.6))
                _style(ax)
                _candles(ax, b, 0)
                pos = {t: k for k, t in enumerate(b.index)}
                vw = v["vwap"].reindex(b.index, method="ffill") if mins == 5 else v["vwap"]
                ax.plot(range(len(b)), vw, color="#6250d6", linewidth=1)
                ax.plot(range(len(b)), b["close"].ewm(span=20, adjust=False).mean(), color=INK2, linewidth=0.8)
                if m:
                    ax.axhspan(float(m.group(2)), float(m.group(3)), color=COLOR["A"], alpha=0.15, linewidth=0)
                if r.first_stop == r.first_stop:
                    ax.axhline(r.first_stop, color="#e34948", linestyle="--", linewidth=0.8)
                for p in tgt[:2]:
                    ax.axhline(p, color="#008300", linestyle=":", linewidth=0.8)
                ke = pos.get(et.floor(f"{mins}min"))
                kx = pos.get(xt.floor(f"{mins}min"))
                if ke is not None:
                    ax.plot(ke, r.entry, "v", color="#e34948", markersize=8)
                if kx is not None:
                    ax.plot(kx, r.exit, "^", color="#008300", markersize=8)
                step = max(1, len(b) // 6)
                ax.set_xticks(range(0, len(b), step))
                ax.set_xticklabels([t.strftime("%H:%M") for t in b.index[::step]])
                ax.set_title(f"{mins}-minute", fontsize=8, color=INK2)
                fig.tight_layout()
                imgs.append(_png(fig))
            cap = (f"{r.symbol} {r.date} {r.strategy}: {r.R:+.2f}R (${r.pnl:+,.0f}), entry {r.entry:.2f} {et:%H:%M}, "
                   f"exit {r.exit:.2f} {xt:%H:%M} ({r.exit_reason}{', ' + r.targets_hit if isinstance(r.targets_hit, str) and r.targets_hit else ''}). "
                   f"{r.reason}")
            out.append((cap, imgs[0], imgs[1]))
    return out


# ------------------------------------------------------------------ page

CSS = """
:root{color-scheme:light;--bg:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--rule:#e4e3df;--pos:#008300;--neg:#c92a2a}
body{background:var(--bg);color:var(--ink);font:14px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;margin:0}
main{max-width:1180px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:22px;margin:0 0 4px} h2{font-size:17px;margin:36px 0 8px;border-top:1px solid var(--rule);padding-top:18px}
h3{font-size:14px;margin:20px 0 6px} p.note{color:var(--ink2);margin:4px 0 10px}
table{border-collapse:collapse;font-size:12.5px;margin:6px 0 14px;font-variant-numeric:tabular-nums}
th,td{padding:3px 10px;border-bottom:1px solid var(--rule);text-align:right} th:first-child,td:first-child{text-align:left}
th{color:var(--ink2);font-weight:600} img{max-width:100%;height:auto}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:12px 24px}
.trade{margin:14px 0 22px} .trade p{font-size:12.5px;color:var(--ink2);margin:2px 0 6px}
.pair{display:grid;grid-template-columns:1fr 1fr;gap:8px} @media(max-width:700px){.pair{grid-template-columns:1fr}}
.wrap{overflow-x:auto}
"""


def _table(df: pd.DataFrame, index_name: str = "") -> str:
    if df is None or df.empty:
        return "<p class='note'>No trades.</p>"
    d = df.copy()
    if index_name:
        d.index.name = index_name
    return "<div class='wrap'>" + d.to_html(border=0, na_rep="–", float_format=lambda x: f"{x:,.3g}" if abs(x) < 10 else f"{x:,.0f}") + "</div>"


def build(name: str, charts: bool = True, n_trade_charts: int = 10) -> str:
    cfg = load_config()
    base = ROOT / "reports" / "backtest" / name
    meta = json.loads((base / "run.json").read_text())
    t = pd.read_csv(base / "trades.csv", parse_dates=["entry_time", "exit_time"])
    t = t[t["R"].notna()]
    eq = cfg["account"]["equity"]
    days = [d for d in sessions("SPY") if pd.Timestamp(meta["start"]) <= d <= pd.Timestamp(meta["end"])]
    split = pd.Timestamp(meta["in_sample_until"])
    d_is = [d for d in days if d < split]
    d_os = [d for d in days if d >= split]
    ins, oos = t[t["sample"] == "in-sample"], t[t["sample"] == "out-of-sample"]

    head = []
    for label, sub_is, sub_os in [("All", ins, oos)] + [(s, ins[ins.strategy == s], oos[oos.strategy == s]) for s in STRATS]:
        for period, sub, dd in (("in-sample", sub_is, d_is), ("out-of-sample", sub_os, d_os)):
            head.append({"strategy": label, "period": period, **metrics(sub, dd, eq)})
    head = pd.DataFrame(head).set_index(["strategy", "period"])

    x = ins.copy()
    x["A0 vs level"] = np.where(x["strategy"] == "A0", "A0 (no level)",
                                np.where(x["strategy"].isin(["A", "B", "C"]), "A/B/C (level)", "D"))
    x["score bucket"] = pd.cut(x["score"], [-1, 4, 7, 10, 15, 999], labels=["<=4", "5-7", "8-10", "11-15", ">15"])
    x["strength bucket"] = pd.cut(x["level_strength"], [-1, 0, 4, 7, 12, 999], labels=["none", "1-4", "5-7", "8-12", ">12"])
    x["peak Vdif $"] = pd.cut(x.get("vdif_peak"), [-99, 0.5, 1, 2, 3, 999], labels=["<0.5", "0.5-1", "1-2", "2-3", ">3"])
    x["add triggered"] = np.where(x["adds"] > 0, "starter + add (full size)", "starter only")
    x["qs before contra"] = np.where(x["qs_to_contra_min"].fillna(999) <= 5, "Quickstrike <= 5 min before Contra",
                                     np.where(x["strategy"] == "A0", "Contra alone", "n/a"))
    x["year"] = pd.to_datetime(x["date"]).dt.year
    src = x.assign(source=x["watchlist_reason"].fillna("").str.split(",")).explode("source")
    src["source"] = src["source"].str.replace(r"intraday:(contra_t\d).*", r"intraday \1", regex=True)
    booster_cols = [c for c in x.columns if c.startswith("b_")]
    bo = []
    for c in booster_cols:
        has = x[c].fillna(0) > 0
        for flag, sub in (("with", x[has]), ("without", x[~has])):
            bo.append({"booster": c[2:], "": flag, "trades": len(sub), "win %": round((sub.pnl > 0).mean() * 100, 1) if len(sub) else np.nan,
                       "avg R": round(sub.R.mean(), 3) if len(sub) else np.nan})
    bo = pd.DataFrame(bo).set_index(["booster", ""]) if bo else pd.DataFrame()

    sections = [
        ("Strategy: A0 (Contra, no level) vs A/B/C (at a level) vs D", breakdown(x, "A0 vs level")),
        ("Focus stocks vs others", breakdown(x, "focus_stock")),
        ("Quickstrike → Contra sequence vs Contra alone (A0)", breakdown(x[x.strategy == "A0"], "qs before contra")),
        ("Peak Vdif in FOCUS ($ above VWAP)", breakdown(x[x.strategy.isin(["A0", "A", "B", "C"])], "peak Vdif $")),
        ("Premarket market tone", breakdown(x, ["premarket_tone", "strategy"])),
        ("Live market tone at entry", breakdown(x, ["live_tone", "strategy"])),
        ("Watchlist source", breakdown(src, "source")),
        ("Level type", breakdown(x, ["level_kind", "strategy"])),
        ("Level strength", breakdown(x, "strength bucket")),
        ("First test vs second test (A/B/C/A0)", breakdown(x[x.strategy != "D"], ["strategy", "test"])),
        ("Starter-only vs full size (A/B/C/A0)", breakdown(x[x.strategy != "D"], ["strategy", "add triggered"])),
        ("Entry trigger", breakdown(x[x.strategy != "D"], "trigger")),
        ("Time of day", breakdown(x, ["time_bucket", "strategy"])),
        ("Setup score", breakdown(x[x.strategy != "D"], "score bucket")),
        ("Contra Short tier on the entry bar", breakdown(x, "contra_tier_at_entry")),
        ("Exit", breakdown(x, ["exit_reason", "targets_hit"])),
        ("Year (in-sample)", breakdown(x, ["year", "strategy"])),
    ]

    html = [f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>Short Bot Backtest</title><style>{CSS}</style></head><body><main>",
            f"<h1>Contrarian short bot: backtest '{name}'</h1>",
            f"<p class='note'>{meta['start']} to {meta['end']}, {meta['days']} sessions, {len(t)} trades, favorites list "
            f"(26 stocks). In-sample until {meta['in_sample_until']}; out-of-sample after. Fixed $1M sizing equity, "
            f"0.5% risk per full position. Data: Nasdaq-exchange 1-minute bars (XNAS.ITCH).</p>",
            "<h2>Headline: each strategy, in-sample vs out-of-sample</h2>", _table(head)]
    if charts:
        html += ["<h2>Equity curve</h2><p class='note'>Cumulative P&L by strategy. The dotted line marks the start of the "
                 "out-of-sample period. The table above holds the same numbers.</p>",
                 f"<img alt='Equity curves by strategy' src='data:image/png;base64,{equity_chart(t, days, split)}'>",
                 "<h2>Distribution of R per trade (all periods)</h2>",
                 f"<img alt='Histogram of R per trade' src='data:image/png;base64,{r_hist(t)}'>"]
    html.append("<h2>Breakdowns (in-sample trades only)</h2><p class='note'>Computed on in-sample trades so that the "
                "out-of-sample period stays untouched for judging any changes. Small groups are noise: read anything "
                "under ~30 trades as anecdote.</p>")
    for title, df in sections:
        html += [f"<h3>{title}</h3>", _table(df)]
    html += ["<h3>With vs without each exhaustion booster (A/B/C/A0)</h3>", _table(bo)]
    sweep = ROOT / "reports" / "backtest" / "sweep" / "summary.csv"
    html.append("<h2>Parameter sensitivity and mode comparisons (in-sample)</h2>")
    if sweep.exists():
        sw = pd.read_csv(sweep)
        html += [f"<p class='note'>Each row changes one setting from the base config and runs on every "
                 f"{int(sw['every'].iloc[0])}th in-sample session ({sw['period'].iloc[0]}), so the numbers are a "
                 f"sample, comparable down the column but not with the headline table. "
                 f"{len(sw)} combinations tried in total.</p>",
                 _table(sw.drop(columns=["every", "period"]).set_index(["group", "variant"]))]
    else:
        html.append("<p class='note'>Not run yet (<code>python -m backtest.sweep</code>).</p>")
    if charts and len(t):
        best = t.sort_values("R", ascending=False).head(n_trade_charts)
        worst = t.sort_values("R").head(n_trade_charts)
        for title, rows in (("10 best trades", best), ("10 worst trades", worst)):
            html.append(f"<h2>{title}</h2><p class='note'>Blue band = level zone, red dashed = first stop, green dotted = "
                        f"T1/T2, violet = VWAP, gray = EMA20. ▼ entry, ▲ final exit.</p>")
            for cap, a, b in trade_charts(rows, load_config()):
                html.append(f"<div class='trade'><p>{cap}</p><div class='pair'><img alt='1-minute chart' "
                            f"src='data:image/png;base64,{a}'><img alt='5-minute chart' src='data:image/png;base64,{b}'></div></div>")
    html.append("</main></body></html>")
    out = base / "report.html"
    out.write_text("\n".join(html))
    return str(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="base")
    ap.add_argument("--no-charts", action="store_true")
    a = ap.parse_args()
    print(build(a.name, charts=not a.no_charts))


if __name__ == "__main__":
    main()
