"""Backtest engine: weekly entries, daily mark-to-market, overlapping positions."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd

from .calendar import Expiration, TradingCalendar, listed_expirations, third_friday
from .chains import CsvChain, Quote, SyntheticChain, forward_price, year_frac
from .config import Config
from .data import MarketData
from .payoff import Strikes, condor_settlement_value, max_loss, max_profit, outcome_bucket
from .pricing import VolSurface
from .products import Product, get_product

# position = +1 L1, -1 L2, -1 L3, +1 L4
SIGNS = (1, -1, -1, 1)


@dataclass
class Position:
    pid: int
    entry_date: date
    exp: Expiration
    strikes: Strikes
    deltas: tuple[float, float, float, float]
    spot: float
    atm_vol: float
    credit_mid: float     # SPX points, at mid
    credit: float         # SPX points, after slippage (what was actually received)
    contracts: int
    fees_usd: float
    max_profit_pts: float
    max_loss_pts: float
    mark: float = 0.0     # current value of the 4 legs (SPX points), negative at entry
    leg_marks: list = field(default_factory=list)  # last known mid per leg
    status: str = "open"
    exit_date: date | None = None
    exit_reason: str = ""
    exit_value: float = math.nan
    settle_price: float = math.nan
    bucket: str = ""
    pnl_usd: float = math.nan
    margin_usd: float = 0.0
    peak_margin_usd: float = 0.0

    def legs(self):
        return list(zip(self.strikes, SIGNS))

    def unrealized_pts(self) -> float:
        return self.credit + self.mark


@dataclass
class Result:
    config: Config
    product: Product
    trades: pd.DataFrame
    daily: pd.DataFrame
    skips: pd.DataFrame
    warnings: list[str] = field(default_factory=list)
    source: str = "synthetic"



class Backtester:
    def __init__(self, cfg: Config, market: MarketData, chain_csv: str | None = None):
        self.cfg = cfg
        self.product = get_product(cfg.product, cfg.product_overrides)
        p = self.product
        for name, w in (("debit_width", cfg.debit_width), ("credit_width", cfg.credit_width)):
            if abs(w / p.strike_step - round(w / p.strike_step)) > 1e-9:
                raise ValueError(
                    f"{name}={w} SPX pts is not a multiple of {p.name}'s strike step "
                    f"({p.strike_step} SPX pts = {p.to_native(p.strike_step):g} native)")
        self.market = market.slice(cfg.start_date, market.dates[-1])
        self.cal = TradingCalendar(self.market.dates)
        self.surface = VolSurface(cfg.surface)
        have_vix = self.market.df["vix"].notna().any()
        self.model = SyntheticChain(p, self.market, self.surface, cfg.div_yield) if have_vix else None
        if chain_csv:
            self.chain = CsvChain(chain_csv, p, self.market, cfg.div_yield)
            self.source = "csv"
        else:
            if self.model is None:
                raise ValueError("synthetic chain needs a 'vix' column in the market data")
            self.chain = self.model
            self.source = "synthetic"
        self.warnings: list[str] = []
        self._warned: set[str] = set()

    # ------------------------------------------------------------------ helpers
    def _warn(self, key: str, msg: str):
        if key not in self._warned:
            self._warned.add(key)
            self.warnings.append(msg)

    def entry_days(self) -> list[date]:
        days = []
        start, end = self.cfg.start_date, self.cfg.end_date
        monday = start - timedelta(days=start.weekday())
        while monday <= end:
            target = monday + timedelta(days=self.cfg.weekday)
            d = self.cal.on_or_before(target)
            if d is not None and d >= monday and start <= d <= end:
                days.append(d)
            monday += timedelta(days=7)
        return days

    def _expirations(self, d: date) -> list[Expiration]:
        if isinstance(self.chain, CsvChain):
            out = []
            for e in self.chain.expirations_on(d):
                tf = e == third_friday(e.year, e.month) or (
                    e.weekday() == 3 and e + timedelta(days=1) == third_friday(e.year, e.month))
                out.append(Expiration(e, tf, tf and e.month in (3, 6, 9, 12)))
            return out
        return listed_expirations(self.product, d, self.cal)

    def _fill(self, mid: float, quote: tuple[float, float] | None, buy: bool) -> float:
        mode = self.cfg.pricing
        if mode == "mid":
            return mid
        if mode == "slippage":
            slip = self.product.from_native(self.cfg.slippage_per_leg)
            return mid + slip if buy else max(mid - slip, 0.0)
        if mode == "natural":
            if quote is None:
                return mid
            return quote[1] if buy else quote[0]
        raise ValueError(f"unknown pricing mode {mode!r}")

    def settlement_price(self, exp: Expiration) -> tuple[float, bool]:
        """(settlement in SPX points of the option's underlying, used_am_proxy_fallback)."""
        p = self.product
        row = self.market.row(exp.date)
        am = (p.settlement == "am"
              or (p.settlement == "am_on_third_friday" and exp.third_friday)
              or (p.settlement == "am_on_quarterly" and exp.quarterly))
        fallback = False
        if am and math.isfinite(row.open):
            s = row.open
        else:
            s = row.close
            fallback = am
        if p.underlying_kind == "futures" and not exp.quarterly:
            s = forward_price(p, s, exp.date, exp.date, row.rate, self.cfg.div_yield)
        return s, fallback

    def _margin(self, pos: Position, d: date) -> float:
        p = self.product
        regt = pos.max_loss_pts * p.usd_per_point * pos.contracts
        if p.margin_model == "regt" or self.model is None:
            return regt
        remaining = (pos.max_loss_pts + pos.unrealized_pts()) * p.usd_per_point * pos.contracts
        legs = pos.legs()
        base = self.model.stressed_value(d, pos.exp.date, legs, 1.0, 1.0)
        worst = 0.0
        for frac in (-1.0, -2 / 3, -1 / 3, 0.0, 1 / 3, 2 / 3, 1.0):
            for vm in (1 - p.span_vol_shift, 1 + p.span_vol_shift):
                v = self.model.stressed_value(d, pos.exp.date, legs, 1 + frac * p.span_scan_range, vm)
                worst = max(worst, base - v)
        span = worst * p.usd_per_point * pos.contracts
        # capped at the defined risk: what is left to lose from here, and never more than the max loss
        return max(min(span, remaining, regt), p.span_min_margin_usd * pos.contracts)

    # ------------------------------------------------------------------ entry
    def try_open(self, d: date, pid: int, open_positions: list[Position]) -> tuple[Position | None, str, dict]:
        cfg, p = self.cfg, self.product
        info: dict = {"entry_date": d}
        exps = [e for e in self._expirations(d) if abs((e.date - d).days - cfg.target_dte) <= cfg.dte_tolerance]
        if not exps:
            listed = self._expirations(d)
            far = max(((e.date - d).days for e in listed), default=0)
            return None, f"no expiration within {cfg.target_dte}+/-{cfg.dte_tolerance} DTE (furthest listed {far})", info
        exp = min(exps, key=lambda e: (abs((e.date - d).days - cfg.target_dte), e.date))
        info.update(expiration=exp.date, dte=(exp.date - d).days)
        quotes = self.chain.puts(d, exp)
        if not quotes:
            return None, "no quotes", info
        by_strike = {q.strike: q for q in quotes}
        q1 = min(quotes, key=lambda q: abs(q.delta + cfg.debit_delta))
        q3 = min(quotes, key=lambda q: abs(q.delta + cfg.credit_delta))
        l1, l3 = q1.strike, q3.strike
        l2 = l1 - cfg.debit_width
        if l3 >= l2:
            if cfg.overlap_rule == "skip" or (cfg.overlap_rule == "allow_equal" and l3 > l2):
                info.update(l1=l1, l2=l2, l3=l3)
                return None, "strike overlap", info
            if cfg.overlap_rule == "shift":
                l3 = l2 - p.strike_step
        l4 = l3 - cfg.credit_width
        k = Strikes(l1, l2, l3, l4)
        info.update(l1=l1, l2=l2, l3=l3, l4=l4)
        if any(s not in by_strike for s in k):
            return None, "strike not listed", info
        qs = [by_strike[s] for s in k]
        mids = [q.mid for q in qs]
        credit_mid = -sum(sign * m for m, sign in zip(mids, SIGNS))
        fills = [self._fill(q.mid, (q.bid, q.ask), buy=sign > 0) for q, sign in zip(qs, SIGNS)]
        credit = -sum(sign * f for f, sign in zip(fills, SIGNS))
        info.update(credit=credit, credit_mid=credit_mid)
        if credit < cfg.min_credit:
            return None, "no credit" if credit <= 0 else f"credit below min_credit ({credit:.2f})", info
        mp, ml = max_profit(k, credit), max_loss(k, credit)
        n = cfg.contracts
        if cfg.max_concurrent_positions is not None and len(open_positions) >= cfg.max_concurrent_positions:
            return None, "max_concurrent_positions", info
        if cfg.max_aggregate_max_loss is not None:
            agg = sum(x.max_loss_pts * p.usd_per_point * x.contracts for x in open_positions)
            if agg + ml * p.usd_per_point * n > cfg.max_aggregate_max_loss:
                return None, "max_aggregate_max_loss", info
        row = self.market.row(d)
        atm = self.surface.atm_vol(year_frac(d, exp.date), row.vix, row.vix3m) if math.isfinite(row.vix) else math.nan
        pos = Position(
            pid=pid, entry_date=d, exp=exp, strikes=k, deltas=tuple(q.delta for q in qs),
            spot=row.close, atm_vol=atm, credit_mid=credit_mid, credit=credit, contracts=n,
            fees_usd=4 * n * p.fee_per_contract_open, max_profit_pts=mp, max_loss_pts=ml,
            mark=sum(sign * m for m, sign in zip(mids, SIGNS)), leg_marks=list(mids),
        )
        return pos, "", info

    # ------------------------------------------------------------------ exits
    def _remark(self, pos: Position, d: date):
        for i, k in enumerate(pos.strikes):
            m = self.chain.mid(d, pos.exp.date, k)
            if m is not None:
                pos.leg_marks[i] = m   # otherwise carry the last mark over a data gap
        pos.mark = sum(sign * m for m, sign in zip(pos.leg_marks, SIGNS))

    def _close_early(self, pos: Position, d: date, reason: str):
        p = self.product
        value = 0.0
        for (k, sign), m in zip(pos.legs(), pos.leg_marks):
            q = self.chain.quote(d, pos.exp.date, k)
            # closing: sell longs (sign>0), buy back shorts
            value += sign * self._fill(m, q, buy=sign < 0)
        pos.status, pos.exit_date, pos.exit_reason, pos.exit_value = "closed", d, reason, value
        pos.fees_usd += 4 * pos.contracts * p.fee_per_contract_close
        pos.bucket = "early_exit"
        pos.pnl_usd = (pos.credit + value) * p.usd_per_point * pos.contracts - pos.fees_usd

    def _settle(self, pos: Position, d: date):
        p = self.product
        s, fallback = self.settlement_price(pos.exp) if self.market.has(pos.exp.date) else (self.market.row(d).close, False)
        if fallback:
            self._warn("am", "AM settlement requested but market data has no 'open'; used the close")
        value = condor_settlement_value(s, pos.strikes)
        itm_legs = sum(1 for k in pos.strikes if k > s)
        pos.status, pos.exit_date, pos.exit_reason = "closed", d, "expiration"
        pos.exit_value, pos.settle_price, pos.bucket = value, s, outcome_bucket(s, pos.strikes)
        pos.fees_usd += itm_legs * pos.contracts * p.settlement_fee
        pos.pnl_usd = (pos.credit + value) * p.usd_per_point * pos.contracts - pos.fees_usd
        pos.mark = value

    def _check_exits(self, pos: Position, d: date) -> str | None:
        cfg = self.cfg
        upnl = pos.unrealized_pts()
        if cfg.take_profit_pct is not None and upnl >= cfg.take_profit_pct * pos.max_profit_pts:
            return "take_profit"
        if cfg.stop_loss_mult is not None and upnl <= -cfg.stop_loss_mult * pos.credit:
            return "stop_loss"
        if cfg.exit_dte is not None and (pos.exp.date - d).days <= cfg.exit_dte:
            return "exit_dte"
        return None

    # ------------------------------------------------------------------ run
    def run(self) -> Result:
        cfg, p = self.cfg, self.product
        entries = set(self.entry_days())
        open_pos: list[Position] = []
        closed: list[Position] = []
        skips: list[dict] = []
        daily: list[dict] = []
        realized = 0.0
        pid = 0
        days = self.cal.between(cfg.start_date, self.market.dates[-1])
        for d in days:
            still_open = []
            for pos in open_pos:
                if d >= pos.exp.date:
                    self._settle(pos, d)
                else:
                    self._remark(pos, d)
                    reason = self._check_exits(pos, d)
                    if reason:
                        self._close_early(pos, d, reason)
                if pos.status == "closed":
                    realized += pos.pnl_usd
                    closed.append(pos)
                else:
                    still_open.append(pos)
            open_pos = still_open

            if d in entries:
                pos, why, info = self.try_open(d, pid, open_pos)
                if pos is None:
                    skips.append({**info, "skip_reason": why})
                else:
                    pid += 1
                    open_pos.append(pos)

            margin = 0.0
            for pos in open_pos:
                pos.margin_usd = self._margin(pos, d)
                pos.peak_margin_usd = max(pos.peak_margin_usd, pos.margin_usd)
                margin += pos.margin_usd
            unreal = sum(x.unrealized_pts() * p.usd_per_point * x.contracts - x.fees_usd for x in open_pos)
            daily.append({
                "date": d, "spx": self.market.row(d).close,
                "realized_pnl": realized, "mtm_pnl": realized + unreal,
                "open_positions": len(open_pos),
                "aggregate_max_loss": sum(x.max_loss_pts * p.usd_per_point * x.contracts for x in open_pos),
                "buying_power": margin,
            })
            if d > cfg.end_date and not open_pos:
                break

        for pos in open_pos:
            pos.bucket, pos.exit_reason = "open", "open at end of data"
            pos.pnl_usd = pos.unrealized_pts() * p.usd_per_point * pos.contracts - pos.fees_usd
        if self.source == "synthetic":
            self._warn("synthetic", "Option prices are MODEL prices (Black-76 + parametric skew from VIX/VIX3M), "
                                    "not historical quotes. Validate with real chains before trading.")
        trades = pd.DataFrame([self._trade_row(x) for x in closed + open_pos])
        return Result(cfg, p, trades, pd.DataFrame(daily), pd.DataFrame(skips), self.warnings, self.source)

    def _trade_row(self, x: Position) -> dict:
        p = self.product
        n = x.contracts
        return {
            "entry_date": x.entry_date, "expiration": x.exp.date, "dte": (x.exp.date - x.entry_date).days,
            "spot_at_entry": round(x.spot, 2),
            "l1": x.strikes.l1, "l2": x.strikes.l2, "l3": x.strikes.l3, "l4": x.strikes.l4,
            "l1_native": p.to_native(x.strikes.l1), "l4_native": p.to_native(x.strikes.l4),
            "d1": round(x.deltas[0], 4), "d2": round(x.deltas[1], 4),
            "d3": round(x.deltas[2], 4), "d4": round(x.deltas[3], 4),
            "atm_vol": round(x.atm_vol, 4) if math.isfinite(x.atm_vol) else math.nan,
            "credit_mid_pts": round(x.credit_mid, 3), "credit_pts": round(x.credit, 3),
            "credit_native": round(p.to_native(x.credit), 3),
            "credit_usd": round(x.credit * p.usd_per_point * n, 2),
            "fees_usd": round(x.fees_usd, 2),
            "max_profit_usd": round(x.max_profit_pts * p.usd_per_point * n, 2),
            "max_loss_usd": round(x.max_loss_pts * p.usd_per_point * n, 2),
            "exit_date": x.exit_date, "exit_reason": x.exit_reason,
            "settle_price": round(x.settle_price, 2) if math.isfinite(x.settle_price) else math.nan,
            "exit_value_pts": round(x.exit_value, 3) if math.isfinite(x.exit_value) else math.nan,
            "outcome": x.bucket, "pnl_usd": round(x.pnl_usd, 2),
            "peak_margin_usd": round(x.peak_margin_usd, 2),
        }
