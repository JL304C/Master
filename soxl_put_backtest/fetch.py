import databento as db, pandas as pd, os
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor
c = db.Historical("x")
ET = ZoneInfo("America/New_York")
daily = c.timeseries.get_range(dataset="XNAS.ITCH", symbols=["SOXL"], schema="ohlcv-1d", start="2018-05-01", end="2026-10-16").to_df()
daily.index = daily.index.tz_convert(None).normalize()
daily[["open","high","low","close","volume"]].to_parquet("cache/daily.parquet")
days = daily.index
# entry day: last trading day of each Mon-Fri week
wk = pd.Series(days, index=days).groupby(days.to_period("W-FRI")).max()
entries = [d for d in wk if d >= pd.Timestamp("2018-05-04")]
print(len(entries), entries[0], entries[-1])
def get(d):
    f = f"cache/q_{d:%Y%m%d}.parquet"
    if os.path.exists(f): return
    t = pd.Timestamp(d.year, d.month, d.day, 15, 45, tzinfo=ET).tz_convert("UTC")
    for attempt in range(4):
        try:
            q = c.timeseries.get_range(dataset="OPRA.PILLAR", symbols=["SOXL.OPT"], stype_in="parent", schema="cbbo-1m", start=t, end=t+pd.Timedelta(minutes=1)).to_df()
            m = c.timeseries.get_range(dataset="XNAS.ITCH", symbols=["SOXL"], schema="ohlcv-1m", start=t-pd.Timedelta(minutes=5), end=t+pd.Timedelta(minutes=1)).to_df()
            q = q[["symbol","bid_px_00","ask_px_00","bid_sz_00","ask_sz_00"]].copy()
            q["spot"] = m["close"].iloc[-1] if len(m) else float("nan")
            q.to_parquet(f); return
        except Exception as e:
            err = e
    print("FAIL", d, err)
with ThreadPoolExecutor(8) as ex: list(ex.map(get, entries))
print("done", len(os.listdir("cache")))
