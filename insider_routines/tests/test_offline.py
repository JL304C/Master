"""
Offline tests: parsing, signal rules and consensus logic against sample
data shaped like the real SEC / Fed / Etherscan responses. No network,
no API keys. Run from insider_routines/:

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ["INSIDER_HOME"] = tempfile.mkdtemp(prefix="insider-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agents"))

import common  # noqa: E402
import eddie  # noqa: E402
import frank  # noqa: E402
import janet  # noqa: E402
import maggie  # noqa: E402
import maya  # noqa: E402
import sophie  # noqa: E402
from common import BEARISH, BULLISH, NEUTRAL, Signal  # noqa: E402

FORM_IDX = """Description:           Daily Index of EDGAR Dissemination Feed by Form Type
Last Data Received:    October 9, 2026

Form Type   Company Name                                                  CIK         Date Filed  File Name
---------------------------------------------------------------------------------------------------------------------------------------------
3           SOME CO                                                       1111111     20261009    edgar/data/1111111/0001111111-26-000001.txt
4           ACME WIDGETS INC                                              2222222     20261009    edgar/data/2222222/0002222222-26-000010.txt
4           DOE JOHN                                                      3333333     20261009    edgar/data/3333333/0002222222-26-000010.txt
4/A         OTHER CO                                                      4444444     20261009    edgar/data/4444444/0004444444-26-000002.txt
4           BETA  CORP                                                    5555555     20261009    edgar/data/5555555/0005555555-26-000003.txt
"""


def form4(ticker="ACME", title="Chief Executive Officer", director="0", code="P", shares="10000", price="150.00", ad="A"):
    return f"""<SEC-DOCUMENT>
<XML>
<?xml version="1.0"?>
<ownershipDocument>
  <issuer><issuerCik>0002222222</issuerCik><issuerName>Acme Widgets Inc</issuerName>
    <issuerTradingSymbol>{ticker}</issuerTradingSymbol></issuer>
  <reportingOwner>
    <reportingOwnerId><rptOwnerCik>0003333333</rptOwnerCik><rptOwnerName>Doe John</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship><isDirector>{director}</isDirector><isOfficer>1</isOfficer>
      <officerTitle>{title}</officerTitle></reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionCoding><transactionFormType>4</transactionFormType><transactionCode>{code}</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>{shares}</value></transactionShares>
        <transactionPricePerShare><value>{price}</value><footnoteId id="F1"/></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>{ad}</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
</XML>
</SEC-DOCUMENT>"""


INFOTABLE = """<?xml version="1.0" encoding="UTF-8"?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
{rows}
</informationTable>"""


def info_row(name, cusip, value, shares, putcall=""):
    pc = f"<putCall>{putcall}</putCall>" if putcall else ""
    return f"""<infoTable><nameOfIssuer>{name}</nameOfIssuer><titleOfClass>COM</titleOfClass>
<cusip>{cusip}</cusip><value>{value}</value>
<shrsOrPrnAmt><sshPrnamt>{shares}</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>{pc}
<investmentDiscretion>SOLE</investmentDiscretion>
<votingAuthority><Sole>{shares}</Sole><Shared>0</Shared><None>0</None></votingAuthority></infoTable>"""


class EddieTests(unittest.TestCase):
    def test_index_keeps_form4_only_deduped(self):
        paths = eddie.parse_form_index(FORM_IDX)
        self.assertEqual(
            paths,
            ["edgar/data/2222222/0002222222-26-000010.txt", "edgar/data/5555555/0005555555-26-000003.txt"],
        )

    def test_ceo_open_market_buy(self):
        buys = eddie.parse_form4(form4(), "f")
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0].ticker, "ACME")
        self.assertAlmostEqual(buys[0].value, 1_500_000)
        self.assertTrue(buys[0].top_exec)

    def test_rejects_sales_grants_and_non_insiders(self):
        self.assertEqual(eddie.parse_form4(form4(code="S", ad="D")), [])
        self.assertEqual(eddie.parse_form4(form4(code="A")), [])  # grant, not open market
        self.assertEqual(eddie.parse_form4(form4(title="VP Marketing")), [])
        self.assertEqual(len(eddie.parse_form4(form4(title="", director="1"))), 1)

    def test_same_trade_in_two_filings_counted_once(self):
        buys = eddie.parse_form4(form4(), "f1") + eddie.parse_form4(form4(), "f2")
        sigs = eddie.build_signals(buys, "2026-10-09")
        self.assertIn("$1,500,000", sigs[0].reason)
        two_trades = eddie.parse_form4(form4(), "f1") + eddie.parse_form4(form4(shares="2000"), "f2")
        self.assertIn("$1,800,000", eddie.build_signals(two_trades, "2026-10-09")[0].reason)

    def test_signal_threshold_and_confidence(self):
        small = eddie.parse_form4(form4(shares="500", price="100"))  # $50k
        self.assertEqual(eddie.build_signals(small, "2026-10-09"), [])
        sigs = eddie.build_signals(eddie.parse_form4(form4()), "2026-10-09")
        self.assertEqual(len(sigs), 1)
        self.assertEqual((sigs[0].ticker, sigs[0].direction, sigs[0].confidence), ("ACME", BULLISH, 5))
        self.assertIn("$1,500,000", sigs[0].reason)


class MaggieTests(unittest.TestCase):
    def test_latest_13f_skips_other_forms(self):
        subs = {"filings": {"recent": {
            "form": ["4", "13F-HR", "SC 13G", "13F-HR/A", "13F-HR"],
            "accessionNumber": ["a", "b", "c", "d", "e"],
            "filingDate": ["2026-10-01", "2026-08-14", "2026-07-01", "2026-06-01", "2026-05-15"],
            "reportDate": ["", "2026-06-30", "", "", "2026-03-31"],
        }}}
        got = maggie.latest_13f(subs)
        self.assertEqual([g["accession"] for g in got], ["b", "e"])

    def test_diff(self):
        prev = maggie.parse_infotable(INFOTABLE.format(rows="".join([
            info_row("APPLE INC", "037833100", 1_000_000_000, 4_000_000),
            info_row("OLD CO", "111111111", 80_000_000, 1_000_000),
            info_row("STEADY CORP", "222222222", 500_000_000, 1_000_000),
        ])))
        cur = maggie.parse_infotable(INFOTABLE.format(rows="".join([
            info_row("APPLE INC", "037833100", 1_500_000_000, 6_000_000),  # +50% shares, +$500M
            info_row("NEW CO", "333333333", 60_000_000, 100_000),
            info_row("STEADY CORP", "222222222", 520_000_000, 1_050_000),  # +5%: ignored
            info_row("NEW CO", "333333333", 90_000_000, 0, putcall="Call"),  # option: ignored
        ])))
        kinds = {m["name"]: m["kind"] for m in maggie.diff_holdings(prev, cur)}
        self.assertEqual(kinds, {"APPLE INC": "INCREASED +50%", "NEW CO": "NEW POSITION", "OLD CO": "EXITED"})
        moves = [
            {**m, "ticker": {"APPLE INC": "AAPL", "NEW CO": "NEWC", "OLD CO": "OLDC"}[m["name"]],
             "fund": "Fund A", "filed": "2026-08-14"}
            for m in maggie.diff_holdings(prev, cur)
        ]
        sigs = {s.ticker: s for s in maggie.build_signals(moves)}
        self.assertEqual(sigs["OLDC"].direction, BEARISH)
        self.assertEqual(sigs["AAPL"].direction, BULLISH)
        self.assertEqual(sigs["NEWC"].confidence, 2)  # $60M + new-position bonus

    def test_index_etfs_ignored(self):
        moves = [
            {"kind": "INCREASED +335%", "usd": 10e9, "ticker": "IVV", "fund": "Citadel", "filed": "2026-08-14"},
            {"kind": "NEW POSITION", "usd": 60e6, "ticker": "NEWC", "fund": "Citadel", "filed": "2026-08-14"},
        ]
        self.assertEqual([s.ticker for s in maggie.build_signals(moves)], ["NEWC"])

    def test_norm_name(self):
        self.assertEqual(maggie.norm_name("Apple Inc."), maggie.norm_name("APPLE INC"))


class FrankTests(unittest.TestCase):
    RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Speech A</title><link>https://www.federalreserve.gov/a.htm</link>
<pubDate>%s</pubDate></item>
<item><title>Old speech</title><link>https://www.federalreserve.gov/old.htm</link>
<pubDate>Mon, 01 Jan 2024 10:00:00 GMT</pubDate></item>
</channel></rss>"""

    def test_feed_window(self):
        now = datetime.now(timezone.utc)
        pub = now.strftime("%a, %d %b %Y %H:%M:%S GMT").encode()
        items = frank.parse_feed(self.RSS % pub, now - timedelta(days=7))
        self.assertEqual([i["title"] for i in items], ["Speech A"])

    def test_page_text_prefers_article(self):
        html = '<html><nav>Menu Home</nav><div id="article"><p>Inflation remains <b>too high</b>.</p><br><div>More.</div></div><footer>x</footer><script>var a=1</script></html>'
        self.assertEqual(frank.page_text(html), "Inflation remains too high. More.")

    def test_unverified_quotes_dropped_and_tally(self):
        docs = [
            {"id": 1, "link": "l1", "title": "t1", "text": "We will keep rates restrictive until inflation is clearly on a path to 2 percent."},
            {"id": 2, "link": "l2", "title": "t2", "text": "Policy is well positioned."},
            {"id": 3, "link": "l3", "title": "t3", "text": "Upside risks to inflation have “diminished”."},
        ]
        results = [
            {"id": 1, "speaker": "A", "stance": "HAWKISH", "summary": "s", "quote": "keep rates restrictive until inflation is clearly on a path"},
            {"id": 2, "speaker": "B", "stance": "DOVISH", "summary": "s", "quote": "we should cut rates soon"},  # invented
            {"id": 3, "speaker": "C", "stance": "HAWKISH", "summary": "s", "quote": 'Upside risks to inflation have "diminished"'},
        ]
        sig, kept = frank.tally(results, docs)
        self.assertEqual([k["id"] for k in kept], [1, 3])
        self.assertEqual((sig.ticker, sig.direction, sig.confidence), ("MACRO", BEARISH, 3))


class MayaTests(unittest.TestCase):
    WALLETS = {"0xcex": "Binance 14"}
    PRICES = {"ETH": 3000.0, "WETH": 3000.0, "WBTC": 100_000.0, "USDC": 1.0, "USDT": 1.0}

    def t(self, h, src, dst, sym, ticker, amount):
        return {"hash": h, "log": "", "from": src, "to": dst, "symbol": sym, "ticker": ticker, "amount": amount}

    def test_flows(self):
        transfers = [
            self.t("1", "0xCEX", "0xwhale", "ETH", "ETH", 4000),  # $12M withdrawn → bullish ETH
            self.t("1", "0xCEX", "0xwhale", "ETH", "ETH", 4000),  # duplicate row (seen from both sides)
            self.t("2", "0xwhale", "0xcex", "USDT", "MACRO", 20_000_000),  # stables in → bullish MACRO
            self.t("3", "0xa", "0xb", "WBTC", "BTC", 500),  # not exchange-related
            self.t("4", "0xcex", "0xc", "WBTC", "BTC", 10),  # $1M — too small
        ]
        moves = maya.classify(transfers, self.WALLETS, self.PRICES)
        self.assertEqual(len(moves), 2)
        sigs = {s.ticker: s for s in maya.build_signals(moves)}
        self.assertEqual(sigs["ETH"].direction, BULLISH)
        self.assertEqual(sigs["MACRO"].direction, BULLISH)
        self.assertNotIn("BTC", sigs)

    def test_wallet_config_loads(self):
        cfg = Path(__file__).resolve().parent.parent / "config"
        orig, maya.CONFIG = maya.CONFIG, cfg
        try:
            wallets = maya.load_wallets()
        finally:
            maya.CONFIG = orig
        self.assertTrue(all(a.startswith("0x") and len(a) == 42 for a in wallets))


class JanetTests(unittest.TestCase):
    def test_drift(self):
        sigs = {s.ticker: s for s in janet.drift_signals(
            {"AAPL": 50, "BTC": 50}, {"AAPL": 7000, "BTC": 3000})}
        self.assertEqual(sigs["AAPL"].direction, BEARISH)
        self.assertEqual(sigs["BTC"].direction, BULLISH)
        self.assertEqual(janet.drift_signals({"AAPL": 50, "BTC": 50}, {"AAPL": 5100, "BTC": 4900}), [])


class SophieTests(unittest.TestCase):
    def setUp(self):
        with common._conn() as c:
            c.execute("DELETE FROM signals")
            c.execute("DELETE FROM consensus")

    def add(self, scout, ticker="NVDA", direction=BULLISH):
        common.record_signals(scout, [Signal(scout, ticker, direction, 3, f"{scout} reason", "raw")])

    def test_fires_once_then_again_only_when_new_scout_joins(self):
        self.add("eddie"); self.add("maggie")
        sophie.main()
        self.assertEqual(len(common.pending_consensus()), 0)  # only 2 scouts

        self.add("janet"); self.add("eddie")  # eddie again: must not double-count
        sophie.main()
        pending = common.pending_consensus()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0][1].scouts, ["eddie", "janet", "maggie"])

        sophie.main()  # next 30-minute run: nothing new
        self.assertEqual(len(common.pending_consensus()), 1)

        self.add("maya")  # a fourth scout joins → one more event
        sophie.main()
        self.assertEqual(len(common.pending_consensus()), 2)

    def test_neutral_and_mixed_directions_ignored(self):
        self.add("eddie"); self.add("maggie"); self.add("janet", direction=BEARISH); self.add("maya", direction=NEUTRAL)
        sophie.main()
        self.assertEqual(common.pending_consensus(), [])


if __name__ == "__main__":
    unittest.main()
