# Insider Routines

Seven small agents that read public market signals and email you when at
least three of them agree. **They never place trades.** The email is
information; the decision is yours.

| Agent | Reads | Source | Schedule |
|-------|-------|--------|----------|
| **Eddie**  | Insider open-market buys ≥ $100k (CEO/CFO/President/Chair/Director) | SEC EDGAR daily index + Form 4 XML | Daily 06:00 |
| **Maggie** | New / +25% / exited positions ≥ $50M at Berkshire, Bridgewater, Renaissance, Citadel, Two Sigma | SEC EDGAR 13F-HR filings, OpenFIGI for tickers | Sun 19:00 |
| **Frank**  | Fed speeches, testimony, FOMC statements from the last 7 days, hawkish vs dovish | federalreserve.gov RSS → **Claude** classifies the text | Mon 08:00 |
| **Maya**   | Whale flows ≥ $5M in/out of exchange wallets (ETH, WETH, WBTC, USDC, USDT) | Etherscan API (free key) | Every 6h |
| **Janet**  | Your portfolio drifting ≥ 5pp from target | Your two JSON files | Daily 17:00 |
| **Sophie** | ≥ 3 scouts agreeing on the same ticker + direction within 7 days | Local SQLite | Every 30m |
| **Ross**   | Emails each new consensus (Telegram optional) | Gmail SMTP | Every 30m |

## How it decides

Each scout **downloads real data with code** and turns it into signals
with fixed rules: ticker, BULLISH/BEARISH, a 1–5 confidence, and a link or
transaction hash for every one. Claude is used where judgment on text is
needed: Frank sends it the full text of each Fed document and asks for a
stance plus an exact supporting quote. Any quote that doesn't appear in the
document gets that classification thrown out. No agent asks Claude to "go
look something up". That would invite made-up filings.

Sophie counts each scout once per ticker + direction, using its latest
signal. A consensus fires **once**. It fires again inside the window only
if a scout that wasn't part of it joins.

**Be realistic about how often you'll hear from Ross.** Stock tickers can
get signals from only three scouts: Eddie, Maggie, and Janet (only if you
hold that ticker). BTC/ETH get signals from Maya and Janet, and MACRO from
Frank and Maya. So a 3-scout consensus is rare by design. Maggie only
reports in the weeks after 13F deadlines (mid-Feb, May, Aug, Nov). You can
lower the bar with `SOPHIE_MIN_AGREE=2` in `.env`.

## Setup (your own computer — Mac, Linux or Windows)

Needs Python 3.10+ (`python3 --version`; on Windows `python --version`).
Get it from https://www.python.org/downloads/ if missing.

```bash
git clone <this repo> && cd <repo>/insider_routines
python3 -m pip install -r requirements.txt   # Windows: python -m pip ...
cp config/.env.example .env                  # Windows: copy config\.env.example .env
```

Open `.env` and fill in:

1. **`ANTHROPIC_API_KEY`**: https://console.anthropic.com/settings/keys
2. **`GMAIL_USER`** + **`GMAIL_APP_PASSWORD`**: an *app password*, not your
   Gmail password: https://myaccount.google.com/apppasswords (requires
   2-Step Verification to be on).
3. **`SEC_USER_AGENT`**: your name and email, e.g. `Jane Doe jane@gmail.com`.
   SEC blocks anonymous requests. If blank, it defaults to your Gmail address.
4. **`ETHERSCAN_API_KEY`** (recommended): free at https://etherscan.io/apis.
   Maya skips without it.
5. Optional: Telegram bot token + chat ID, OpenFIGI key.

**Janet (optional):** copy the two example files and edit them to match
your real portfolio. Until both exist, Janet skips. The example numbers are
deliberately off-target, so don't leave them as they are.

```bash
cp config/portfolio_target.example.json  config/portfolio_target.json
cp config/portfolio_current.example.json config/portfolio_current.json
```

**Maya's wallet list** (`config/cex_wallets.json`) is a starting set of
well-known exchange wallets. Check each address's label on etherscan.io
before you rely on it, and add or remove wallets as you like.

### Smoke test

`--dry-run` prints signals without saving them:

```bash
python3 agents/frank.py  --dry-run   # proves the Anthropic key works (~1 Claude call)
python3 agents/eddie.py  --dry-run   # scans yesterday's Form 4s, a few minutes
python3 agents/maggie.py --dry-run --force   # diffs each fund's latest 13F
python3 agents/maya.py   --dry-run
python3 agents/sophie.py && python3 agents/ross.py
```

### Turn on the schedule

```bash
bash install/schedule_mac.sh       # macOS (launchd)
bash install/schedule_linux.sh     # Linux (crontab)
powershell -ExecutionPolicy Bypass -File install\schedule_windows.ps1   # Windows
```

Undo with the matching `install/uninstall_*` script. Jobs run only while
the computer is awake. launchd and Task Scheduler catch up on a missed run
after wake-up; cron doesn't.

Logs: `.state/logs/<agent>.log`. Database: `.state/state.db`.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The offline tests cover the parsing, signal rules and consensus logic
against sample SEC / Fed / Etherscan data. They don't call live services.

## Costs

The only paid call is Frank's: one Claude request a week, about the size of
that week's Fed speeches. Model and effort are set by `INSIDER_MODEL` /
`INSIDER_EFFORT`. SEC, the Fed, OpenFIGI, CoinGecko, and Etherscan's free
tier cost nothing.
