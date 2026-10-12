"""Fetcher routing. Clients are fakes; urllib is not allowed to run."""

import json
import os
import pathlib
import unittest
import urllib.request

from Scouts.accumulation import fetcher
from Scouts.accumulation.fetcher import (
    asx_form_from_headline,
    choose_primary_name,
    collect_asx,
    collect_sec,
    efts_url,
    fetch_ticker_map,
)
from Scouts.accumulation.forms import SEC_FORM_TYPES
from Scouts.accumulation.pipeline import analyse, to_json
from Scouts.accumulation.filters import ScreenConfig
from shared.nethttp import EndpointError
from datetime import date

FIX = pathlib.Path(__file__).resolve().parents[1] / "fixtures"


class FakeClient:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def json(self, url):
        self.calls.append(("json", url))
        if url not in self.routes:
            raise AssertionError(url)
        return self.routes[url]

    def get(self, url, accept="application/json"):
        self.calls.append(("get", url, accept))
        if url not in self.routes:
            raise AssertionError(url)
        body = self.routes[url]
        if isinstance(body, str):
            body = body.encode()
        return body, "application/xml"


class FetcherTests(unittest.TestCase):
    def setUp(self):
        self._urlopen = urllib.request.urlopen
        urllib.request.urlopen = self._refuse

    def tearDown(self):
        urllib.request.urlopen = self._urlopen

    @staticmethod
    def _refuse(*_args, **_kwargs):
        raise AssertionError("live network")

    def test_efts_url_includes_legacy_and_xml_form_types(self):
        url = efts_url("2026-09-01", "2026-09-30", SEC_FORM_TYPES, 0)
        self.assertTrue(url.startswith("https://efts.sec.gov/LATEST/search-index?"))
        for form in ("SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A", "SCHEDULE 13D", "SCHEDULE 13D/A", "SCHEDULE 13G", "SCHEDULE 13G/A"):
            self.assertIn(form.replace(" ", "+").replace("/", "%2F"), url)

    def test_collect_sec_uses_the_client_and_primary_doc(self):
        index = json.loads((FIX / "sec_index.json").read_text(encoding="utf-8"))
        body = (FIX / "sec_13d.xml").read_text(encoding="utf-8")
        page = {
            "hits": {
                "total": {"value": 1},
                "hits": [
                    {
                        "_source": {
                            "adsh": "0000999001-26-000001",
                            "ciks": ["0000999001"],
                            "form": "SCHEDULE 13D",
                            "file_date": "2026-09-16",
                        }
                    }
                ],
            }
        }
        search = efts_url("2026-09-01", "2026-09-30", SEC_FORM_TYPES, 0)
        doc_url = "https://www.sec.gov/Archives/edgar/data/999001/000099900126000001/primary_doc.xml"
        index_url = "https://www.sec.gov/Archives/edgar/data/999001/000099900126000001/index.json"
        client = FakeClient({search: page, index_url: index, doc_url: body})
        documents = collect_sec("2026-09-01", "2026-09-30", client=client)
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0].url, doc_url)
        self.assertEqual(documents[0].form_type, "SCHEDULE 13D")
        self.assertEqual(documents[0].filing_date, "2026-09-16")
        self.assertEqual(documents[0].accession, "0000999001-26-000001")
        kinds = [call[0] for call in client.calls]
        self.assertEqual(kinds, ["json", "json", "get"])

    def test_xsl_path_is_only_a_fallback(self):
        self.assertEqual(choose_primary_name(["xslSCHEDULE_13D_X01/primary_doc.xml", "primary_doc.xml"]), "primary_doc.xml")
        self.assertEqual(choose_primary_name(["xslSCHEDULE_13G_X01/primary_doc.xml"]), "xslSCHEDULE_13G_X01/primary_doc.xml")

    def test_ticker_map(self):
        payload = json.loads((FIX / "company_tickers.json").read_text(encoding="utf-8"))
        client = FakeClient({"https://www.sec.gov/files/company_tickers.json": payload})
        mapping = fetch_ticker_map(client)
        self.assertEqual(mapping["0000999101"], "NWND")
        self.assertEqual(mapping["0000999102"], "BRMB")

    def test_asx_headlines_and_download(self):
        self.assertEqual(asx_form_from_headline("Becoming a substantial holder"), "603")
        self.assertEqual(asx_form_from_headline("Change in substantial holding"), "604")
        self.assertEqual(asx_form_from_headline("Ceasing to be a substantial holder"), "605")
        self.assertIsNone(asx_form_from_headline("Dividend reinvestment plan"))
        announcements = json.loads((FIX / "asx_announcements.json").read_text(encoding="utf-8"))
        notice = (FIX / "asx_603.txt").read_text(encoding="utf-8")
        routes = {"https://www.asx.com.au/asx/1/company/NWND/announcements?count=50": announcements}
        for row in announcements["data"]:
            routes[row["url"]] = notice
        client = FakeClient(routes)
        documents = collect_asx(["nwnd"], client=client)
        self.assertEqual([doc.form_type for doc in documents], ["603", "604", "605"])
        self.assertEqual(documents[0].filing_date, "2026-09-04")
        self.assertEqual(documents[0].ticker, "NWND")
        self.assertTrue(all(call[1].endswith("asx.com.au") or "asx.com.au/" in call[1] for call in client.calls))

    def test_refuses_a_document_host_outside_asx(self):
        url = "https://www.asx.com.au/asx/1/company/NWND/announcements?count=50"
        payload = {"data": [{"header": "Becoming a substantial holder", "url": "https://example.com/notice", "document_release_date": "2026-09-04"}]}
        client = FakeClient({url: payload})
        with self.assertRaises(EndpointError):
            collect_asx(["NWND"], client=client)
        self.assertEqual(client.calls, [("json", url)])

    def test_default_sec_client_refuses_before_any_socket(self):
        os.environ.pop("SEC_CONTACT_EMAIL", None)
        with self.assertRaises(EndpointError):
            collect_sec("2026-09-01", "2026-09-30")

    def test_analyse_fixture_round_trip(self):
        body = (FIX / "sec_13d.xml").read_text(encoding="utf-8")
        from Scouts.accumulation.model import Document
        doc = Document(
            source="sec",
            body=body,
            url="https://www.sec.gov/Archives/edgar/data/999001/000099900126000001/primary_doc.xml",
            form_type="SCHEDULE 13D",
            filing_date="2026-09-16",
            accession="0000999001-26-000001",
        )
        report = analyse(
            [doc, doc],
            ScreenConfig(as_of=date(2026, 10, 1), recent_days=30),
            {"0000999101": "NWND"},
            {"NWND": "materials"},
        )
        self.assertEqual(len(report["candidates"]), 1)
        text = to_json(report)
        self.assertIn('"scout": "accumulation"', text)
        parsed = json.loads(text)
        self.assertEqual(parsed["candidates"][0]["stake_pct"], 7.5)


if __name__ == "__main__":
    unittest.main()
