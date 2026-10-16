"""Statistics, tables and charts."""
from __future__ import annotations

import numpy as np
import pandas as pd

SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e6e5e1"
BLUE, RED, MID = "#2a78d6", "#e34948", "#f0efec"


def stats(trades: pd.DataFrame, days: pd.DataFrame | None = None, risk: float = 0.01) -> dict:
    n = len(trades)
    out = {"trades": n}
    if days is not None and len(days):
        tradable = days[~days.outcome.isin(["roll", "zero_height", "skipped_size"])]
        no_setup = ["no_break", "no_snapback", "no_pullback", "no_rejection", "range_broken"]
        out["no_setup_pct"] = 100 * tradable.outcome.isin(no_setup).mean() if len(tradable) else np.nan
    if n == 0:
        return out
    R = trades["R"].to_numpy()
    wins, losses = R[R > 0], R[R <= 0]
    avg_w = wins.mean() if len(wins) else 0.0
    avg_l = losses.mean() if len(losses) else 0.0
    eq = np.cumprod(1 + risk * R)
    dd = (eq / np.maximum.accumulate(np.concatenate([[1], eq]))[1:] - 1).min()
    streak = cur = 0
    for r in R:
        cur = cur + 1 if r <= 0 else 0
        streak = max(streak, cur)
    # Sharpe on per-session returns (0 on days without a trade), annualised with 252 sessions
    if days is not None and len(days):
        daily = pd.Series(0.0, index=days["date"].unique())
        daily.loc[trades["date"].to_numpy()] = risk * R
        sharpe = daily.mean() / daily.std() * np.sqrt(252) if daily.std() > 0 else np.nan
    else:
        sharpe = np.nan
    gross_win, gross_loss = wins.sum(), -losses.sum()
    top5 = np.sort(R)[::-1][:5].sum()
    out.update({
        "win_rate": 100 * len(wins) / n,
        "breakeven_win_rate": 100 * -avg_l / (avg_w - avg_l) if avg_w - avg_l > 0 else np.nan,
        "avg_win_R": avg_w, "avg_loss_R": avg_l,
        "expectancy_R": R.mean(),
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else np.inf,
        "total_R": R.sum(),
        "return_pct": 100 * (eq[-1] - 1),
        "max_dd_pct": 100 * dd,
        "longest_losing_streak": streak,
        "sharpe": sharpe,
        "expectancy_without_top5_R": (R.sum() - top5) / max(n - 5, 1),
    })
    return out


def buy_and_hold(bars5: pd.DataFrame, start, end) -> dict:
    """1x long, daily closes, ignoring the jump on contract-roll days."""
    b = bars5[(bars5.index >= start) & (bars5.index < end)]
    if b.empty:
        return {}
    daily = b.resample("1D").agg({"close": "last", **({"instrument_id": "last"} if "instrument_id" in b else {})}).dropna()
    ret = daily["close"].pct_change()
    if "instrument_id" in daily:
        ret[daily["instrument_id"] != daily["instrument_id"].shift()] = 0.0
    ret = ret.fillna(0.0)
    eq = (1 + ret).cumprod()
    years = max((daily.index[-1] - daily.index[0]).days / 365.25, 1e-9)
    return {
        "return_pct": 100 * (eq.iloc[-1] - 1),
        "cagr_pct": 100 * (eq.iloc[-1] ** (1 / years) - 1),
        "max_dd_pct": 100 * (eq / eq.cummax() - 1).min(),
        "sharpe": ret.mean() / ret.std() * np.sqrt(252) if ret.std() > 0 else np.nan,
    }


def breakdown(trades: pd.DataFrame, by: str) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    key = trades["date"].dt.year if by == "year" else trades["date"].dt.day_name().str[:3]
    g = trades.groupby(key)["R"]
    df = pd.DataFrame({"trades": g.size(), "win_rate": 100 * g.apply(lambda r: (r > 0).mean()),
                       "expectancy_R": g.mean(), "total_R": g.sum()})
    if by == "weekday":
        df = df.reindex([d for d in ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"] if d in df.index])
    return df


def fmt_table(rows: dict[str, dict], cols: list[str]) -> str:
    head = "| | " + " | ".join(cols) + " |\n|---|" + "---|" * len(cols) + "\n"
    body = ""
    for name, r in rows.items():
        cells = []
        for c in cols:
            v = r.get(c, "")
            cells.append(f"{v:.2f}" if isinstance(v, (float, np.floating)) else str(v))
        body += f"| {name} | " + " | ".join(cells) + " |\n"
    return head + body


def df_md(df: pd.DataFrame) -> str:
    if df.empty:
        return "_no trades_\n"
    cols = [str(c) for c in df.columns]
    lines = ["| | " + " | ".join(cols) + " |", "|---|" + "---|" * len(cols)]
    for idx, row in df.iterrows():
        cells = [f"{v:.2f}" if isinstance(v, (float, np.floating)) else str(v) for v in row]
        lines.append(f"| {idx} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def _style(ax):
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK2, labelsize=9)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def equity_chart(trades: pd.DataFrame, split_date, title: str, path, risk: float = 0.01):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(10, 4.2), facecolor=SURFACE)
    _style(ax)
    if not trades.empty:
        eq = 100 * (np.cumprod(1 + risk * trades["R"].to_numpy()) - 1)
        ax.plot(trades["date"], eq, color=BLUE, linewidth=2)
        ax.axhline(0, color=INK2, linewidth=0.8)
        ax.axvline(split_date, color=INK2, linewidth=1, linestyle="--")
        ax.text(split_date, ax.get_ylim()[1], "  out-of-sample →", color=INK2, va="top", fontsize=9)
    ax.set_title(title, color=INK, fontsize=12, loc="left")
    ax.set_ylabel("Return, % (1% risk per trade)", color=INK2, fontsize=9)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def heatmap_chart(grid: pd.DataFrame, title: str, path, row_name: str = "stop_k", col_name: str = "be_trigger"):
    """grid: rows/cols are two parameters' values, cells are expectancy in R."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import LinearSegmentedColormap, TwoSlopeNorm

    cmap = LinearSegmentedColormap.from_list("div", [RED, MID, BLUE])
    vals = grid.to_numpy(float)
    lim = max(np.nanmax(np.abs(vals)), 1e-6)
    fig, ax = plt.subplots(figsize=(5.6, 4), facecolor=SURFACE)
    ax.imshow(vals, cmap=cmap, norm=TwoSlopeNorm(0, -lim, lim), aspect="auto")
    for i in range(vals.shape[0]):
        for j in range(vals.shape[1]):
            ax.text(j, i, f"{vals[i, j]:+.3f}R", ha="center", va="center", color=INK, fontsize=10)
    ax.set_xticks(range(len(grid.columns)), [str(c) for c in grid.columns])
    ax.set_yticks(range(len(grid.index)), [str(r) for r in grid.index])
    ax.set_xlabel(f"{col_name} (None = off)", color=INK2, fontsize=9)
    ax.set_ylabel(row_name, color=INK2, fontsize=9)
    ax.tick_params(colors=INK2, labelsize=9, length=0)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.set_title(title, color=INK, fontsize=11, loc="left")
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)
