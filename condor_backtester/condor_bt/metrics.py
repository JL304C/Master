"""Summary statistics and the risk items from spec section 7."""
from __future__ import annotations

import math

import pandas as pd

from .engine import Result


def _f(x, nd=2):
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else round(float(x), nd)


def summarize(res: Result) -> dict:
    t, daily, skips, p, cfg = res.trades, res.daily, res.skips, res.product, res.config
    closed = t[t["outcome"] != "open"] if len(t) else t
    open_ = t[t["outcome"] == "open"] if len(t) else t
    wins = closed[closed["pnl_usd"] > 0] if len(closed) else closed
    losses = closed[closed["pnl_usd"] <= 0] if len(closed) else closed
    total_closed = float(closed["pnl_usd"].sum()) if len(closed) else 0.0
    total_mtm = float(daily["mtm_pnl"].iloc[-1]) if len(daily) else 0.0

    in_window = daily[pd.to_datetime(daily["date"]).dt.date <= cfg.end_date] if len(daily) else daily
    cap_a = float(daily["aggregate_max_loss"].max()) if len(daily) else 0.0   # Reg-T style
    cap_b = float(daily["buying_power"].max()) if len(daily) else 0.0          # modeled margin peak
    years = max((daily["date"].iloc[-1] - daily["date"].iloc[0]).days / 365.25, 1e-9) if len(daily) else math.nan

    def cagr(cap):
        if not cap or not math.isfinite(years):
            return None
        end = (cap + total_mtm) / cap
        return (end ** (1 / years) - 1) if end > 0 else -1.0

    mtm = daily["mtm_pnl"] if len(daily) else pd.Series(dtype=float)
    dd = mtm - mtm.cummax()
    max_dd = float(dd.min()) if len(dd) else 0.0
    dd_date = daily["date"].iloc[int(dd.idxmin())] if len(dd) else None
    real = daily["realized_pnl"] if len(daily) else pd.Series(dtype=float)
    max_dd_real = float((real - real.cummax()).min()) if len(real) else 0.0

    spx0, spx1 = (in_window["spx"].iloc[0], in_window["spx"].iloc[-1]) if len(in_window) else (math.nan, math.nan)

    loss_months = {}
    if len(losses):
        lm = losses.assign(month=pd.to_datetime(losses["expiration"]).dt.strftime("%Y-%m"))
        g = lm.groupby("month")["pnl_usd"].agg(["count", "sum"])
        loss_months = {m: {"count": int(r["count"]), "pnl_usd": _f(r["sum"])} for m, r in g.iterrows()}

    pos_delta = None
    if len(t):
        pos_delta = float((t["d1"] - t["d2"] - t["d3"] + t["d4"]).mean())

    gross_credit = float(t["credit_usd"].sum()) if len(t) else 0.0
    fees = float(t["fees_usd"].sum()) if len(t) else 0.0
    max_loss_trade = float(closed["pnl_usd"].min()) if len(closed) else None

    return {
        "product": p.name,
        "usd_per_spx_point": p.usd_per_point,
        "chain_source": res.source,
        "window": f"{cfg.start_date} -> {cfg.end_date} (positions run to expiry; data to {daily['date'].iloc[-1] if len(daily) else '?'})",
        "params": {k: getattr(cfg, k) for k in ("target_dte", "dte_tolerance", "debit_delta", "debit_width",
                                                 "credit_delta", "credit_width", "min_credit", "overlap_rule",
                                                 "pricing", "slippage_per_leg", "take_profit_pct",
                                                 "stop_loss_mult", "exit_dte", "contracts")},
        "entries_attempted": int(len(t) + len(skips)),
        "trades": int(len(t)),
        "closed_trades": int(len(closed)),
        "open_at_end": int(len(open_)),
        "skipped": int(len(skips)),
        "skip_reasons": skips["skip_reason"].value_counts().to_dict() if len(skips) else {},
        "win_rate": _f(len(wins) / len(closed), 4) if len(closed) else None,
        "wins": int(len(wins)), "losses": int(len(losses)),
        "avg_win_usd": _f(wins["pnl_usd"].mean()) if len(wins) else None,
        "avg_loss_usd": _f(losses["pnl_usd"].mean()) if len(losses) else None,
        "worst_trade_usd": _f(max_loss_trade),
        "full_max_loss_trades": int((closed["outcome"] == "max_loss").sum()) if len(closed) else 0,
        "outcomes": closed["outcome"].value_counts().to_dict() if len(closed) else {},
        "avg_credit_pts": _f(t["credit_pts"].mean(), 3) if len(t) else None,
        "avg_credit_usd": _f(t["credit_usd"].mean()) if len(t) else None,
        "avg_l3_pct_below_spot": _f(100 * (1 - t["l3"] / t["spot_at_entry"]).mean(), 1) if len(t) else None,
        "loss_expiry_months": " ".join(f"{m}({v['count']})" for m, v in loss_months.items()),
        "total_pnl_closed_usd": _f(total_closed),
        "total_pnl_mtm_usd": _f(total_mtm),
        "fees_usd": _f(fees),
        "fees_pct_of_gross_credit": _f(100 * fees / gross_credit, 1) if gross_credit else None,
        "max_drawdown_mtm_usd": _f(max_dd), "max_drawdown_mtm_date": str(dd_date),
        "max_drawdown_realized_usd": _f(max_dd_real),
        "max_concurrent_positions": int(daily["open_positions"].max()) if len(daily) else 0,
        "worst_case_aggregate_loss_usd": _f(cap_a),
        "peak_buying_power_usd": _f(cap_b),
        "capital_regt_usd": _f(cap_a),
        "roc_regt_pct": _f(100 * total_mtm / cap_a, 1) if cap_a else None,
        "roc_peak_bp_pct": _f(100 * total_mtm / cap_b, 1) if cap_b else None,
        "cagr_on_regt_capital_pct": _f(100 * cagr(cap_a), 2) if cap_a else None,
        "cagr_on_peak_bp_pct": _f(100 * cagr(cap_b), 2) if cap_b else None,
        "max_dd_pct_of_regt_capital": _f(100 * max_dd / cap_a, 1) if cap_a else None,
        "avg_trade_roc_on_max_loss_pct": _f(100 * (closed["pnl_usd"] / closed["max_loss_usd"]).mean(), 2) if len(closed) else None,
        "avg_position_delta_at_entry": _f(pos_delta, 4),
        "spx_buy_hold_pct": _f(100 * (spx1 / spx0 - 1), 1) if spx0 == spx0 else None,
        "losses_by_expiration_month": loss_months,
        "warnings": res.warnings,
    }
