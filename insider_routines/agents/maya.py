#!/usr/bin/env python3
"""
Maya — the on-chain whale-watcher.

Uses the Etherscan API (free key) to read every transfer in/out of a list
of known exchange wallets (config/cex_wallets.json) since her last run.
Transfers >= $5M are classified:
  exchange → outside wallet  = ACCUMULATION (withdrawn to hold)
  outside wallet → exchange  = DISTRIBUTION (deposited, likely to sell)
and netted per asset:
  ETH / WETH, WBTC  → signal on ETH / BTC (net outflow BULLISH, inflow BEARISH)
  USDC / USDT       → signal on MACRO (net inflow = buying power BULLISH)

Coverage: Ethereum mainnet only. Native bitcoin is not tracked.

Schedule: every 6 hours.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (
    BEARISH,
    BULLISH,
    CONFIG,
    Signal,
    confidence_steps,
    http_json,
    kv_get,
    kv_set,
    log,
    record_signals,
    run_agent,
)

API = "https://api.etherscan.io/v2/api"
MIN_TRANSFER_USD = 5_000_000
WINDOW_SECONDS = 6 * 3600

# contract → (symbol, decimals, signal ticker)
TOKENS = {
    "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599": ("WBTC", 8, "BTC"),
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": ("WETH", 18, "ETH"),
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": ("USDC", 6, "MACRO"),
    "0xdac17f958d2ee523a2206206994597c13d831ec7": ("USDT", 6, "MACRO"),
}
PAGE = 10_000  # Etherscan's maximum page size


def load_wallets() -> dict[str, str]:
    """address (lowercase) → label."""
    path = CONFIG / "cex_wallets.json"
    data = json.loads(path.read_text())
    return {addr.lower(): label for label, addr in data.items() if not label.startswith("_")}


def classify(transfers: list[dict], wallets: dict[str, str], prices: dict[str, float]) -> list[dict]:
    """Keep exchange↔outside transfers >= $5M. Each transfer: from, to, symbol, amount, hash."""
    out = []
    seen: set[tuple[str, str, str]] = set()
    for t in transfers:
        key = (t["hash"], t["symbol"], t.get("log", ""))
        if key in seen:
            continue
        seen.add(key)
        src, dst = t["from"].lower(), t["to"].lower()
        if (src in wallets) == (dst in wallets):
            continue  # exchange↔exchange or unrelated
        usd = t["amount"] * prices.get(t["symbol"], 0)
        if usd < MIN_TRANSFER_USD:
            continue
        flow = "OUT" if src in wallets else "IN"
        out.append({**t, "usd": usd, "flow": flow, "exchange": wallets.get(src) or wallets.get(dst)})
    return out


def build_signals(moves: list[dict]) -> list[Signal]:
    per: dict[str, list[dict]] = {}
    for m in moves:
        per.setdefault(m["ticker"], []).append(m)
    sigs = []
    for ticker, group in per.items():
        net_out = sum(m["usd"] if m["flow"] == "OUT" else -m["usd"] for m in group)
        if abs(net_out) < MIN_TRANSFER_USD:
            continue
        if ticker == "MACRO":
            # Stablecoins arriving on exchanges = dry powder to buy with.
            direction = BULLISH if net_out < 0 else BEARISH
            what = "stablecoins net " + ("into" if net_out < 0 else "out of") + " exchanges"
        else:
            direction = BULLISH if net_out > 0 else BEARISH
            what = f"{ticker} net " + ("withdrawn from" if net_out > 0 else "deposited to") + " exchanges"
        conf = confidence_steps(abs(net_out), (25e6, 50e6, 100e6, 250e6))
        reason = f"${abs(net_out) / 1e6:,.0f}M {what} in {len(group)} whale transfer(s), last 6h"
        raw = json.dumps(
            [{k: m[k] for k in ("hash", "symbol", "usd", "flow", "exchange")} for m in group], indent=1
        )
        sigs.append(Signal("maya", ticker, direction, conf, reason, raw))
    return sigs


def _get(params: dict, key: str) -> list | str:
    data = http_json(API + "?" + "&".join(f"{k}={v}" for k, v in {**params, "chainid": 1, "apikey": key}.items()))
    if data.get("status") == "1":
        return data["result"]
    if isinstance(data.get("result"), list) or "No transactions found" in str(data.get("message")):
        return []
    raise RuntimeError(f"Etherscan error: {data.get('message')} {data.get('result')}")


def prices_usd() -> dict[str, float]:
    p = http_json("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd")
    eth, btc = float(p["ethereum"]["usd"]), float(p["bitcoin"]["usd"])
    return {"ETH": eth, "WETH": eth, "WBTC": btc, "USDC": 1.0, "USDT": 1.0}


def main() -> int:
    key = os.environ.get("ETHERSCAN_API_KEY")
    if not key:
        print("[maya] ETHERSCAN_API_KEY not set — skipping (free key: https://etherscan.io/apis)")
        log("maya", "no ETHERSCAN_API_KEY, skipped")
        return 0
    wallets = load_wallets()
    prices = prices_usd()

    now = int(time.time())
    end_block = int(_get({"module": "block", "action": "getblocknobytime", "timestamp": now, "closest": "before"}, key))
    start_block = int(
        _get({"module": "block", "action": "getblocknobytime", "timestamp": now - WINDOW_SECONDS, "closest": "after"}, key)
    )
    last = kv_get("maya.last_block")
    if last:
        start_block = max(start_block, int(last) + 1)

    transfers: list[dict] = []
    span = {"startblock": start_block, "endblock": end_block, "page": 1, "offset": PAGE, "sort": "desc"}
    for addr, label in wallets.items():
        for contract, (sym, dec, ticker) in TOKENS.items():
            rows = _get({"module": "account", "action": "tokentx", "contractaddress": contract, "address": addr, **span}, key)
            if len(rows) == PAGE:
                log("maya", f"{label} {sym}: hit the {PAGE}-row page limit; oldest transfers in the window skipped")
            for r in rows:
                transfers.append(
                    {"hash": r["hash"], "log": r.get("logIndex", ""), "from": r["from"], "to": r["to"],
                     "symbol": sym, "ticker": ticker, "amount": int(r["value"]) / 10**dec}
                )
        rows = _get({"module": "account", "action": "txlist", "address": addr, **span}, key)
        for r in rows:
            if r.get("isError") == "0" and int(r["value"]) > 0:
                transfers.append(
                    {"hash": r["hash"], "log": "", "from": r["from"], "to": r["to"],
                     "symbol": "ETH", "ticker": "ETH", "amount": int(r["value"]) / 1e18}
                )

    moves = classify(transfers, wallets, prices)
    sigs = build_signals(moves)
    if sigs:
        record_signals("maya", sigs)
    else:
        print(f"[maya] no net whale flow ≥ $5M across {len(wallets)} exchange wallets")
        log("maya", f"no qualifying whale flow (blocks {start_block}-{end_block})")
    kv_set("maya.last_block", str(end_block))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_agent("maya", main))
