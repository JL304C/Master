"""Flag overnight price jumps that look like stock splits, and say whether
splits.yaml already lists them.

    python -m tools.check_splits
"""
from __future__ import annotations

from data.bars import daily_bars, splits_for
from data.fetch import ROOT, load_config

RATIOS = [2, 3, 4, 5, 6, 8, 10, 15, 20, 25, 30, 40, 50]


def candidates(symbol: str, tol: float = 0.06):
    d = daily_bars(symbol)
    r = (d["close"].shift(1) / d["open"]).dropna()
    out = []
    for day, x in r.items():
        for k in RATIOS:
            for target in (k, 1 / k):
                if abs(x / target - 1) <= tol:
                    out.append((day, target, x))
    return out


def main() -> None:
    cfg = load_config()
    syms = [p.stem for p in (ROOT / cfg["data"]["cache_dir"] / cfg["data"]["daily_dataset"]).glob("*.parquet")
            if not p.stem.endswith(".daily")]
    bad = 0
    for s in sorted(syms):
        known = {d.date(): r for d, r in splits_for(s)}
        for day, target, x in candidates(s):
            ok = day.date() in known and abs(known[day.date()] / target - 1) < 0.01
            bad += not ok
            print(f"{s:6s} {day:%Y-%m-%d} looks like {target:g}-for-1 (prev close/open = {x:.3f})  "
                  f"{'listed' if ok else 'NOT IN splits.yaml'}")
        for d, r in known.items():
            if not any(c[0].date() == d for c in candidates(s)):
                print(f"{s:6s} {d} listed {r:g}-for-1 but no matching price jump found")
                bad += 1
    print("all splits accounted for" if not bad else f"{bad} item(s) to check")


if __name__ == "__main__":
    main()
