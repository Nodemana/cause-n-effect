"""Fixture tests for insider and CEO buying. No network. Synthetic names only."""

import json
import os
import sys
import unittest
import urllib.request
from pathlib import Path

SCOUT = Path(__file__).resolve().parents[1]
REPO = SCOUT.parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(SCOUT))

import fetcher  # noqa: E402
import parser  # noqa: E402
import pipeline  # noqa: E402
from filters import ScreenConfig  # noqa: E402

FIX = SCOUT / "fixtures"
URL = "https://www.sec.gov/Archives/edgar/data/999001/form4.xml"


def _xml(code, acquired, shares, price, owner="Bramble Holt", title="", director="0", officer="0", form="4", when="2026-03-10", extra=""):
    title_xml = f"<officerTitle>{title}</officerTitle>" if title or officer == "1" else ""
    if officer == "1" and not title:
        title_xml = "<officerTitle>Chief Financial Officer</officerTitle>"
    price_xml = "<transactionPricePerShare><footnoteId id='F1'/></transactionPricePerShare>" if price is None else f"<transactionPricePerShare><value>{price}</value></transactionPricePerShare>"
    return f"""<?xml version="1.0"?>
<ownershipDocument>
  <documentType>{form}</documentType>
  <issuer>
    <issuerName>Northwind Grid</issuerName>
    <issuerTradingSymbol>NWT</issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>{owner}</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>{director}</isDirector>
      <isOfficer>{officer}</isOfficer>
      <isTenPercentOwner>0</isTenPercentOwner>
      {title_xml}
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionDate><value>{when}</value></transactionDate>
      <transactionCoding>
        <transactionCode>{code}</transactionCode>
        <equitySwapInvolved>0</equitySwapInvolved>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>{shares}</value></transactionShares>
        {price_xml}
        <transactionAcquiredDisposedCode><value>{acquired}</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
  {extra}
</ownershipDocument>
"""


class InsiderBuyingTests(unittest.TestCase):
    def setUp(self):
        self._urlopen = urllib.request.urlopen
        urllib.request.urlopen = self._refuse

    def tearDown(self):
        urllib.request.urlopen = self._urlopen

    @staticmethod
    def _refuse(*_args, **_kwargs):
        raise AssertionError("live network")

    def test_ceo_open_market_purchase_is_included(self):
        body = (FIX / "sec_form4_ceo.xml").read_text(encoding="utf-8")
        table = pipeline.build_table([
            {"source": "sec", "body": body, "url": URL, "filing_date": "2026-03-04", "ticker": "NWT"},
        ])
        self.assertEqual(table["scout"], "insider-buying")
        self.assertEqual(len(table["records"]), 1)
        self.assertEqual(table["review"], [])
        row = table["records"][0]
        self.assertEqual(row["insider"], "Harbour Lane")
        self.assertTrue(row["is_ceo"])
        self.assertIn("Chief Executive Officer", row["role"])
        self.assertIn("director", row["role"])
        self.assertEqual(row["ticker"], "NWT")
        self.assertEqual(row["company"], "Northwind Grid")
        self.assertEqual(row["shares"], 2000)
        self.assertEqual(row["price"], 40)
        self.assertEqual(row["value"], 80000)
        self.assertEqual(row["currency"], "USD")
        self.assertEqual(row["transaction_date"], "2026-03-02")
        self.assertEqual(row["filing_date"], "2026-03-04")
        self.assertEqual(row["source_url"], URL)
        self.assertEqual(row["form"], "4")
        self.assertFalse(row["cluster"])
        figure = table["figures"][0]
        self.assertEqual(figure["figure_type"], "insider_purchase")
        self.assertEqual(figure["instrument"], "ceo")
        self.assertEqual(figure["period"], "2026-03-02|Harbour Lane")
        self.assertEqual(figure["value"], 80000)
        self.assertEqual(figure["source_type"], "filing")
        self.assertEqual(figure["verified"], 1)
        parsed = json.loads(pipeline.to_json(table))
        self.assertEqual(parsed["records"][0]["is_ceo"], True)

    def test_sales_grants_exercises_and_small_buys_are_excluded(self):
        documents = [
            {"source": "sec", "body": _xml("S", "D", 100, 10), "url": URL, "filing_date": "2026-03-11"},
            {"source": "sec", "body": _xml("A", "A", 100, 0, title="VP", officer="1"), "url": URL, "filing_date": "2026-03-11"},
            {"source": "sec", "body": _xml("M", "A", 100, 5), "url": URL, "filing_date": "2026-03-11"},
            {"source": "sec", "body": _xml("G", "A", 100, 5), "url": URL, "filing_date": "2026-03-11"},
            {"source": "sec", "body": _xml("P", "A", 100, 20, owner="Small Buyer", when="2026-03-12"), "url": URL, "filing_date": "2026-03-12"},
        ]
        table = pipeline.build_table(documents, ScreenConfig(min_value=10_000))
        self.assertEqual(table["records"], [])
        self.assertEqual(table["review"], [])

    def test_missing_price_and_amendment_and_joint_filing(self):
        missing = parser.parse_form4(_xml("P", "A", 1000, None), URL, "2026-03-04")
        self.assertIn("missing_value", missing[0]["flags"])
        amended = parser.parse_form4(
            _xml("P", "A", 1000, 30, form="4/A", owner="Harbour Lane", title="Chief Executive Officer", officer="1", director="1"),
            URL,
            "2026-03-05",
        )
        self.assertEqual(amended[0]["form"], "4/A")
        self.assertTrue(amended[0]["is_ceo"])
        joint = """<?xml version="1.0"?>
<ownershipDocument>
  <documentType>4</documentType>
  <issuer><issuerName>Northwind Grid</issuerName><issuerTradingSymbol>NWT</issuerTradingSymbol></issuer>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>Harbour Lane</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship><isOfficer>1</isOfficer><officerTitle>CEO</officerTitle></reportingOwnerRelationship>
  </reportingOwner>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>Copperline Reed</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship><isTenPercentOwner>1</isTenPercentOwner></reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-03-02</value></transactionDate>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>1000</value></transactionShares>
        <transactionPricePerShare><value>50</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""
        table = pipeline.build_table([
            {"source": "sec", "body": _xml("P", "A", 1000, None), "url": URL, "filing_date": "2026-03-04"},
            {"source": "sec", "body": joint, "url": URL, "filing_date": "2026-03-04"},
        ])
        self.assertEqual(table["records"], [])
        self.assertGreaterEqual(len(table["review"]), 2)
        self.assertTrue(any("joint_filing" in row["flags"] for row in table["review"]))
        self.assertTrue(any(row["role"] == "10% owner" for row in table["review"]))

    def test_cluster_of_distinct_insiders(self):
        documents = [
            {"source": "sec", "body": _xml("P", "A", 1000, 20, owner="Harbour Lane", title="Chief Executive Officer", officer="1", director="1", when="2026-03-01"), "url": URL, "filing_date": "2026-03-02"},
            {"source": "sec", "body": _xml("P", "A", 500, 22, owner="Copperline Reed", director="1", when="2026-03-05"), "url": URL, "filing_date": "2026-03-06"},
            {"source": "sec", "body": _xml("P", "A", 800, 21, owner="Harbour Lane", title="Chief Executive Officer", officer="1", when="2026-04-20"), "url": URL, "filing_date": "2026-04-21"},
        ]
        table = pipeline.build_table(documents, ScreenConfig(min_value=10_000, cluster_window_days=7))
        by_day = {row["transaction_date"]: row for row in table["records"]}
        self.assertTrue(by_day["2026-03-01"]["cluster"])
        self.assertTrue(by_day["2026-03-01"]["is_ceo"])
        self.assertTrue(by_day["2026-03-05"]["cluster"])
        self.assertFalse(by_day["2026-03-05"]["is_ceo"])
        self.assertFalse(by_day["2026-04-20"]["cluster"])

    def test_asx_on_market_kept_and_off_market_dropped(self):
        on_market = (FIX / "asx_3y_onmarket.txt").read_text(encoding="utf-8")
        off_market = (FIX / "asx_3y_offmarket.txt").read_text(encoding="utf-8")
        blank = """Appendix 3Y
Name of entity: Northwind Grid Limited
Name of Director: Bramble Holt
Date of change: 02/04/2026
Number acquired: 1000
Value/Consideration: $40,000
Nature of change:
"""
        table = pipeline.build_table([
            {"source": "asx", "body": on_market, "url": "https://asx.api.markitdigital.com/asx-research/1.0/file/example", "filing_date": "2026-03-15", "ticker": "NWT.AX", "company": "Northwind Grid Limited"},
            {"source": "asx", "body": off_market, "url": "https://asx.api.markitdigital.com/asx-research/1.0/file/off", "filing_date": "2026-03-16", "ticker": "NWT.AX"},
            {"source": "asx", "body": blank, "url": "https://asx.api.markitdigital.com/asx-research/1.0/file/blank", "filing_date": "2026-04-02", "ticker": "NWT.AX"},
        ])
        self.assertEqual(len(table["records"]), 1)
        row = table["records"][0]
        self.assertEqual(row["insider"], "Copperline Reed")
        self.assertTrue(row["is_ceo"])
        self.assertEqual(row["shares"], 1500)
        self.assertEqual(row["value"], 30000)
        self.assertEqual(row["currency"], "AUD")
        self.assertEqual(row["transaction_date"], "2026-03-15")
        self.assertEqual(row["price"], 20)
        self.assertEqual(table["figures"][0]["source_type"], "asx_announcement")
        self.assertEqual(len(table["review"]), 1)
        self.assertIn("ambiguous_item", table["review"][0]["flags"])
        self.assertEqual(table["review"][0]["insider"], "Bramble Holt")

    def test_fetcher_uses_the_client_and_refuses_other_hosts(self):
        body = (FIX / "sec_form4_ceo.xml").read_text(encoding="utf-8")
        index = json.loads((FIX / "sec_index.json").read_text(encoding="utf-8"))
        page = {
            "hits": {
                "total": {"value": 1},
                "hits": [{
                    "_source": {
                        "adsh": "0000999001-26-000004",
                        "ciks": ["999001"],
                        "form": "4",
                        "file_date": "2026-03-04",
                        "display_names": ["Northwind Grid  (NWT)  (CIK 0000999001)"],
                    }
                }],
            }
        }
        calls = []

        class Fake:
            def json(self, url):
                calls.append(url)
                if "search-index" in url:
                    return page
                if url.endswith("/index.json"):
                    return index
                raise AssertionError(url)

            def get(self, url, accept=""):
                calls.append(url)
                return body, "application/xml"

        documents = fetcher.collect_sec("2026-03-01", "2026-03-31", client=Fake())
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0]["ticker"], "NWT")
        self.assertIn("doc4.xml", documents[0]["url"])
        self.assertTrue(all("sec.gov" in url for url in calls))
        table = pipeline.build_table(documents)
        self.assertEqual(table["records"][0]["insider"], "Harbour Lane")
        with self.assertRaises(fetcher.EndpointError):
            fetcher._require_sec("https://example.com/form4.xml")
        with self.assertRaises(fetcher.EndpointError):
            fetcher._require_asx("https://example.com/3y")

    def test_asx_list_keeps_3y_only(self):
        payload = {
            "data": {
                "displayName": "NORTHWIND GRID",
                "items": [
                    {"date": "2026-03-15T01:00:00.000Z", "headline": "Appendix 3Y - Change of Director's Interest Notice", "documentKey": "key-3y"},
                    {"date": "2026-03-16T01:00:00.000Z", "headline": "Dividend", "documentKey": "key-div"},
                ],
            }
        }

        class Fake:
            def json(self, url):
                return payload

            def get(self, url, accept=""):
                self.url = url
                return (FIX / "asx_3y_onmarket.txt").read_text(encoding="utf-8"), "text/plain"

        fake = Fake()
        documents = fetcher.collect_asx([{"ticker": "NWT"}], client=fake)
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0]["ticker"], "NWT.AX")
        self.assertIn("key-3y", documents[0]["url"])
        self.assertIn("Copperline Reed", documents[0]["body"])

    def test_default_sec_client_refuses_before_any_socket(self):
        from shared.nethttp import EndpointError

        os.environ.pop("SEC_CONTACT_EMAIL", None)
        with self.assertRaises(EndpointError):
            fetcher.collect_sec("2026-03-01", "2026-03-31")


if __name__ == "__main__":
    unittest.main()
