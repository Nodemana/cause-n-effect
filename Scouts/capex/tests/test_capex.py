"""Fixture tests for the capex scout. No network."""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path

SCOUT = Path(__file__).resolve().parents[1]
REPO = SCOUT.parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(SCOUT))

import fetcher  # noqa: E402
import live  # noqa: E402
import pipeline  # noqa: E402
from money import first_amount  # noqa: E402

FIX = SCOUT / "fixtures"
AMZN = {"ticker": "AMZN", "name": "Amazon.com Inc", "cik": "0001018724"}


class Fake:
    def __init__(self, routes):
        self.routes = routes
        self.urls = []

    def json(self, url):
        self.urls.append(url)
        if url not in self.routes:
            raise AssertionError(url)
        return self.routes[url]


class CapexTests(unittest.TestCase):
    def test_companyfacts_keep_reported_actuals(self):
        payload = json.loads((FIX / "companyfacts_amzn.json").read_text(encoding="utf-8"))
        client = Fake({fetcher.companyfacts_url(AMZN["cik"]): payload})
        found = fetcher.collect_companyfacts([AMZN], client=client)
        facts = found[0]["facts"]
        tags = {(item["tag"], item["row"]["end"], item["row"].get("start")) for item in facts}
        self.assertEqual(
            tags,
            {
                ("PaymentsToAcquireProductiveAssets", "2025-03-31", "2025-01-01"),
                ("PaymentsToAcquireProductiveAssets", "2025-06-30", "2025-01-01"),
                ("PaymentsToAcquireProductiveAssets", "2026-06-30", "2025-07-01"),
                ("FinanceLeaseRightOfUseAsset", "2025-03-31", None),
            },
        )
        q1 = next(
            item
            for item in facts
            if item["tag"] == "PaymentsToAcquireProductiveAssets" and item["row"]["end"] == "2025-03-31"
        )
        self.assertEqual(q1["row"]["val"], 24000000000)
        self.assertEqual(q1["row"]["filed"], "2025-04-15")
        self.assertEqual(q1["row"]["accn"], "0001018724-25-000001")
        self.assertEqual(q1["period_label"], "quarter")
        half = next(
            item
            for item in facts
            if item["tag"] == "PaymentsToAcquireProductiveAssets" and item["row"]["end"] == "2025-06-30"
        )
        self.assertEqual(half["row"]["val"], 48000000000)
        self.assertEqual(half["row"]["filed"], "2025-08-01")
        self.assertEqual(half["period_label"], "ytd")
        trailing = next(
            item
            for item in facts
            if item["tag"] == "PaymentsToAcquireProductiveAssets" and item["row"]["end"] == "2026-06-30"
        )
        self.assertEqual(trailing["row"]["val"], 90000000000)
        self.assertEqual(trailing["row"]["filed"], "2026-07-15")
        self.assertEqual(trailing["period_label"], "ttm")
        self.assertEqual(fetcher.infer_fiscal_start(payload), "01-01")
        self.assertTrue(client.urls[0].startswith("https://data.sec.gov/api/xbrl/companyfacts/CIK0001018724.json"))
        with self.assertRaises(fetcher.EndpointError):
            fetcher._require_sec("https://example.com/companyfacts")

    def test_guidance_search_uses_efts_phrases(self):
        page = json.loads((FIX / "efts_guidance.json").read_text(encoding="utf-8"))
        url = fetcher.efts_url(AMZN["cik"], "2025-05-01", "2025-08-01")
        client = Fake({url: page})
        hits = fetcher.collect_guidance([AMZN], client=client, start="2025-05-01", end="2025-08-01")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(client.urls[0]).query)
        self.assertIn("capital expenditure", query["q"][0])
        self.assertIn("capex", query["q"][0])
        self.assertEqual(query["ciks"], ["0001018724"])
        self.assertEqual(hits[0]["hit"]["_source"]["form"], "8-K")
        with self.assertRaises(fetcher.EndpointError):
            fetcher._require_sec("https://example.com/search")

    def test_asx_list_is_capped_at_five_and_health_checked(self):
        payload = json.loads((FIX / "asx_announcements.json").read_text(encoding="utf-8"))
        calls = []

        class Recording(Fake):
            def json(self, url):
                calls.append(url)
                return payload

        rows = fetcher.collect_asx([{"ticker": "exg", "name": "Example Grid Ltd"}], client=Recording({}), count=40)
        self.assertIn("count=5", calls[0])
        self.assertTrue(calls[0].startswith("https://asx.api.markitdigital.com/asx-research/1.0/companies/exg/"))
        self.assertEqual([row["document_key"] for row in rows], ["2924-00000001-2A1", "2924-00000003-2A1"])
        self.assertNotIn("2924-00000006-2A1", [row["document_key"] for row in rows])
        from shared.nethttp import ASX_MIN_INTERVAL, asx_client

        self.assertEqual(ASX_MIN_INTERVAL, 2.0)
        self.assertEqual(asx_client().min_interval, 2.0)
        with self.assertRaises(fetcher.EndpointError):
            fetcher.check_feed({"unexpected": True}, "EXG")
        with self.assertRaises(fetcher.EndpointError):
            fetcher._require_asx("https://example.com/announcements")

    def test_pipeline_writes_shared_figures(self):
        facts_payload = json.loads((FIX / "companyfacts_amzn.json").read_text(encoding="utf-8"))
        guidance_page = json.loads((FIX / "efts_guidance.json").read_text(encoding="utf-8"))
        facts_url = fetcher.companyfacts_url(AMZN["cik"])
        guidance_url = fetcher.efts_url(AMZN["cik"], "2025-05-01", "2025-08-01")
        client = Fake({facts_url: facts_payload, guidance_url: guidance_page})
        facts = fetcher.collect_companyfacts([AMZN], client=client)
        guidance = fetcher.collect_guidance([AMZN], client=client, start="2025-05-01", end="2025-08-01")
        asx_payload = json.loads((FIX / "asx_announcements.json").read_text(encoding="utf-8"))
        announcements = fetcher.collect_asx(
            [{"ticker": "EXG"}],
            client=type("C", (), {"json": lambda self, url: asx_payload})(),
        )
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "history.sqlite")
            summary = pipeline.run(
                db,
                facts,
                guidance,
                announcements,
                started_at="2026-10-12T00:00:00Z",
                seen_at="2026-10-12",
                notes="fixture",
            )
            self.assertEqual(summary["run_id"], "2026-10-12-capex")
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            run = conn.execute("SELECT scout, notes FROM runs").fetchone()
            self.assertEqual(run["scout"], "capex")
            rows = conn.execute(
                """
                SELECT ticker, figure_type, period, value, currency, announced_on,
                       source_url, source_type, verified
                FROM figures
                """
            ).fetchall()
            by_key = {(row["ticker"], row["figure_type"], row["period"]): row for row in rows}
            actual = by_key[("AMZN", "PaymentsToAcquireProductiveAssets", "quarter|2025-01-01/2025-03-31")]
            self.assertEqual(actual["value"], 24000000000)
            self.assertEqual(actual["currency"], "USD")
            self.assertEqual(actual["announced_on"], "2025-04-15")
            ytd = by_key[("AMZN", "PaymentsToAcquireProductiveAssets", "ytd|2025-01-01/2025-06-30")]
            self.assertEqual(ytd["value"], 48000000000)
            self.assertEqual(ytd["announced_on"], "2025-08-01")
            trailing = by_key[("AMZN", "PaymentsToAcquireProductiveAssets", "ttm|2025-07-01/2026-06-30")]
            self.assertEqual(trailing["value"], 90000000000)
            self.assertEqual(trailing["announced_on"], "2026-07-15")
            self.assertEqual(actual["source_type"], "filing")
            self.assertEqual(actual["verified"], 1)
            self.assertIn("/Archives/edgar/data/1018724/", actual["source_url"])
            guide = by_key[("AMZN", "capex_guidance", "2025-06-30")]
            self.assertEqual(guide["value"], 75_000_000_000)
            self.assertEqual(guide["currency"], "USD")
            self.assertEqual(guide["announced_on"], "2025-07-31")
            asx = by_key[("EXG.AX", "capex_announcement", "2026-03-02")]
            self.assertEqual(asx["value"], 40_000_000)
            self.assertEqual(asx["currency"], "AUD")
            self.assertEqual(asx["source_type"], "asx_announcement")
            self.assertTrue(asx["source_url"].startswith("https://asx.api.markitdigital.com/"))
            conn.close()

    def test_span_labels_follow_the_fiscal_year(self):
        self.assertEqual(fetcher.span_label("2025-01-01", "2025-03-31", "01-01"), "quarter")
        self.assertEqual(fetcher.span_label("2025-04-01", "2025-06-30", "01-01"), "quarter")
        self.assertEqual(fetcher.span_label("2025-01-01", "2025-06-30", "01-01"), "ytd")
        self.assertEqual(fetcher.span_label("2025-01-01", "2025-12-31", "01-01"), "annual")
        self.assertEqual(fetcher.span_label("2025-07-01", "2026-06-30", "01-01"), "ttm")
        self.assertEqual(fetcher.span_label("2025-07-01", "2026-06-30", "07-01"), "annual")
        self.assertEqual(fetcher.span_label("2025-07-01", "2026-03-31", "07-01"), "ytd")
        june_year = {
            "facts": {
                "us-gaap": {
                    "PaymentsToAcquirePropertyPlantAndEquipment": {
                        "units": {
                            "USD": [
                                {
                                    "start": "2025-07-01",
                                    "end": "2025-09-30",
                                    "val": 10,
                                    "accn": "0000000001-25-000001",
                                    "fy": 2026,
                                    "fp": "Q1",
                                    "form": "10-Q",
                                    "filed": "2025-11-01",
                                },
                                {
                                    "start": "2025-07-01",
                                    "end": "2026-06-30",
                                    "val": 40,
                                    "accn": "0000000001-26-000001",
                                    "fy": 2026,
                                    "fp": "FY",
                                    "form": "10-K",
                                    "filed": "2026-08-15",
                                },
                                {
                                    "start": "2025-07-01",
                                    "end": "2026-06-30",
                                    "val": 41,
                                    "accn": "0000000001-26-000099",
                                    "fy": 2026,
                                    "fp": "FY",
                                    "form": "10-K/A",
                                    "filed": "2026-09-01",
                                },
                            ]
                        }
                    }
                }
            }
        }
        self.assertEqual(fetcher.infer_fiscal_start(june_year), "07-01")
        facts = fetcher.select_facts(june_year, n=4)
        by_end = {item["row"]["end"]: item for item in facts}
        self.assertEqual(by_end["2025-09-30"]["period_label"], "quarter")
        self.assertEqual(by_end["2026-06-30"]["period_label"], "annual")
        self.assertEqual(by_end["2026-06-30"]["row"]["val"], 40)
        self.assertEqual(by_end["2026-06-30"]["row"]["filed"], "2026-08-15")

    def test_amounts_and_sec_email_gate(self):
        self.assertEqual(first_amount("$75 billion")["value"], 75_000_000_000)
        self.assertEqual(first_amount("A$40 million", dollar="AUD")["currency"], "AUD")
        previous = os.environ.pop("SEC_CONTACT_EMAIL", None)
        try:
            self.assertEqual(
                live.sec_block_message(),
                "SEC_CONTACT_EMAIL is missing. No SEC request was sent.",
            )
        finally:
            if previous is not None:
                os.environ["SEC_CONTACT_EMAIL"] = previous
        text = (SCOUT / "live.py").read_text(encoding="utf-8")
        self.assertNotIn("example.invalid", text)
        self.assertIn("sec_client", (SCOUT / "fetcher.py").read_text(encoding="utf-8"))

    def test_live_fixture_stores_a_figure_and_empty_run_exits_nonzero(self):
        import live

        previous = os.environ.pop("SEC_CONTACT_EMAIL", None)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                db = str(Path(tmp) / "history.sqlite")
                code = live.main(
                    [
                        "--db",
                        db,
                        "--sec-fixture",
                        str(FIX / "companyfacts_amzn.json"),
                        "--no-asx",
                    ]
                )
                self.assertEqual(code, 0)
                conn = sqlite3.connect(db)
                count = conn.execute("SELECT COUNT(*) FROM figures").fetchone()[0]
                conn.close()
                self.assertGreater(count, 0)
                empty = live.main(["--db", str(Path(tmp) / "empty.sqlite"), "--no-asx"])
                self.assertEqual(empty, 1)
        finally:
            if previous is not None:
                os.environ["SEC_CONTACT_EMAIL"] = previous


if __name__ == "__main__":
    unittest.main()
