"""Fixture tests for the debt-raise use-of-proceeds gate. No network."""

import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path

SCOUT = Path(__file__).resolve().parents[1]
REPO = SCOUT.parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(SCOUT))

import fetcher  # noqa: E402
import pipeline  # noqa: E402
import report  # noqa: E402
from score import first_amount, histogram, load_config, needs_full_document, score_text  # noqa: E402

FIX = SCOUT / "fixtures"
ACC_GENERIC = "0000999001-26-000010"
ACC_CENTER = "0000999001-26-000020"
ACC_THIN = "0000999001-26-000011"
ACC_BOTH = "0000999001-26-000021"
ACC_CAPEX = "0000999001-26-000030"
ACC_OFFICER = "0000999001-26-000099"


def _filing(accession, form, summary, items=None, ticker="NWT"):
    return {
        "accession": accession,
        "cik": "0000999001",
        "ticker": ticker,
        "form": form,
        "items": items or [],
        "filed": "2026-03-01",
        "summary": summary,
        "document_url": f"https://www.sec.gov/Archives/edgar/data/999001/{accession}-index.html",
    }


class DebtRaiseTests(unittest.TestCase):
    def setUp(self):
        self._urlopen = urllib.request.urlopen
        urllib.request.urlopen = self._refuse
        self.config = load_config()
        self.rules = self.config["rules"]

    def tearDown(self):
        urllib.request.urlopen = self._urlopen

    @staticmethod
    def _refuse(*_args, **_kwargs):
        raise AssertionError("live network")

    def test_weights_and_threshold_gate(self):
        generic = score_text("Proceeds for general corporate purposes only.", self.rules)
        center = score_text("Financing for data center construction in Ohio.", self.rules)
        capex = score_text("<p>capital expenditures at the plant</p>", self.rules)
        acquire = score_text("The notes finance the acquisition of a transformer business.", self.rules)
        boilerplate = score_text(
            "We may use the net proceeds for general corporate purposes, which may include acquisitions of complementary businesses.",
            self.rules,
        )
        blank = score_text("   ", self.rules)
        unknown = score_text("Item 1.01 Entry into a Material Definitive Agreement.", self.rules)
        both = score_text(
            "general corporate purposes, including data center construction",
            self.rules,
        )

        self.assertEqual(generic.score, 0)
        self.assertTrue(generic.decided)
        self.assertEqual(center.score, 5)
        self.assertEqual(capex.score, 3)
        self.assertEqual(acquire.score, 4)
        self.assertEqual(boilerplate.score, 0)
        self.assertNotIn("acquisition", boilerplate.matched)
        self.assertEqual(both.score, 5)
        self.assertIn("generic", both.matched)
        self.assertFalse(blank.decided)
        self.assertIsNone(unknown.score)

        threshold = self.config["threshold"]
        self.assertEqual(threshold, 2)
        self.assertFalse(needs_full_document(generic, threshold))
        self.assertTrue(needs_full_document(center, threshold))
        self.assertTrue(needs_full_document(capex, threshold))
        self.assertTrue(needs_full_document(unknown, threshold))
        self.assertFalse(needs_full_document(capex, 4))
        self.assertFalse(needs_full_document(generic, 0))
        self.assertTrue(needs_full_document(score_text("working capital", self.rules), 0))

    def test_run_reads_full_documents_only_when_the_cheap_filter_says_so(self):
        calls = []

        class Fake:
            def json(self, url):
                calls.append(("json", url))
                raise AssertionError(url)

            def get(self, url, accept=""):
                calls.append(("get", url))
                raise AssertionError(url)

        filings = [
            _filing(ACC_GENERIC, "8-K", "Proceeds will be used for general corporate purposes.", ["1.01"]),
            _filing(ACC_CENTER, "8-K", "Financing for data center construction.", ["2.03"]),
            _filing(ACC_THIN, "424B5", "Prospectus supplement"),
            _filing(ACC_BOTH, "8-K/A", "general corporate purposes, including data center construction", ["1.01"]),
            _filing(ACC_OFFICER, "8-K", "acquisition of a business", ["5.02"]),
        ]
        documents = {
            ACC_CENTER: "The facility funds data center construction of US$180 million.",
            ACC_THIN: (FIX / "full_capex.txt").read_text(encoding="utf-8"),
            ACC_BOTH: (FIX / "full_generic.html").read_text(encoding="utf-8"),
        }
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "history.sqlite")
            summary = pipeline.run(
                filings,
                db,
                client=Fake(),
                documents=documents,
                started_at="2026-03-04T12:00:00Z",
            )
            self.assertEqual(calls, [])
            self.assertEqual(summary["filings_seen"], 4)
            self.assertEqual(summary["resolved_by_cheap_filter"], 1)
            self.assertEqual(summary["sent_to_full_document"], 3)
            self.assertEqual(summary["run_id"], "2026-03-04-120000-debt-raise")
            self.assertEqual(summary["score_histogram"]["0"], 2)
            self.assertEqual(summary["score_histogram"]["3"], 1)
            self.assertEqual(summary["score_histogram"]["5+"], 1)
            self.assertEqual(summary["score_histogram"]["undecided"], 0)

            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            run = conn.execute(
                """
                SELECT r.scout, r.started_at, d.threshold, d.filings_seen,
                       d.resolved_by_cheap_filter, d.sent_to_full_document, d.score_histogram
                FROM debt_scout_runs d
                JOIN runs r ON r.run_id = d.run_id
                """
            ).fetchone()
            self.assertEqual(run["scout"], "debt-raise")
            self.assertEqual(run["started_at"], "2026-03-04T12:00:00Z")
            self.assertEqual(run["threshold"], 2)
            self.assertEqual(json.loads(run["score_histogram"]), summary["score_histogram"])
            rows = conn.execute(
                """
                SELECT accession, ticker, score, stage_reached, cheap_score, decided, flags
                FROM debt_scout_filing_scores
                ORDER BY accession
                """
            ).fetchall()
            by_acc = {row["accession"]: row for row in rows}
            self.assertEqual(by_acc[ACC_GENERIC]["stage_reached"], "cheap_filter")
            self.assertEqual(by_acc[ACC_GENERIC]["score"], 0)
            self.assertEqual(by_acc[ACC_THIN]["stage_reached"], "full_document")
            self.assertEqual(by_acc[ACC_THIN]["score"], 3)
            self.assertIsNone(by_acc[ACC_THIN]["cheap_score"])
            self.assertEqual(by_acc[ACC_CENTER]["score"], 5)
            self.assertEqual(by_acc[ACC_CENTER]["stage_reached"], "full_document")
            self.assertEqual(by_acc[ACC_BOTH]["cheap_score"], 5)
            self.assertEqual(by_acc[ACC_BOTH]["score"], 0)
            self.assertEqual(by_acc[ACC_BOTH]["stage_reached"], "full_document")
            self.assertNotIn(ACC_OFFICER, by_acc)
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertTrue(
                {"runs", "figures", "leads", "debt_scout_runs", "debt_scout_filing_scores"} <= tables
            )
            views = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'view'")
            }
            self.assertIn("changes", views)
            figure = conn.execute(
                "SELECT ticker, figure_type, value, value_text, currency, verified, source_type FROM figures"
            ).fetchone()
            self.assertEqual(figure["ticker"], "NWT")
            self.assertEqual(figure["figure_type"], "facility_amount")
            self.assertEqual(figure["value"], 180_000_000)
            self.assertEqual(figure["currency"], "USD")
            self.assertEqual(figure["verified"], 1)
            self.assertEqual(figure["source_type"], "filing")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0], 0)
            conn.close()

            text = report.render(db)
            self.assertIn("threshold", text)
            self.assertIn("2026-03-04T12:00:00Z", text)
            self.assertIn("5+=1", text)

    def test_higher_threshold_leaves_capex_on_the_cheap_filter(self):
        filings = [
            _filing(ACC_CAPEX, "424B2", "Proceeds fund capital expenditures at the plant."),
            _filing(ACC_CENTER, "8-K", "Proceeds fund data center construction.", ["1.01"]),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "history.sqlite")
            pipeline.run(
                filings,
                db,
                threshold=4,
                documents={ACC_CENTER: "data center construction"},
                started_at="2026-04-01T00:00:00Z",
            )
            pipeline.run(
                [_filing(ACC_GENERIC, "8-K", "general corporate purposes", ["2.03"])],
                db,
                threshold=2,
                started_at="2026-04-02T00:00:00Z",
            )
            text = report.render(db)
            self.assertIn("4 ", text)
            self.assertIn("3 ", text)
            self.assertIn("Cheap-filter scores inside the mid band", text)
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                report.main(["--db", db])
            self.assertEqual(buffer.getvalue(), text)
            conn = sqlite3.connect(db)
            mid = conn.execute(
                """
                SELECT s.score, s.stage_reached
                FROM debt_scout_filing_scores s
                JOIN debt_scout_runs d ON d.run_id = s.run_id
                WHERE d.threshold = 4 AND s.accession = ?
                """,
                (ACC_CAPEX,),
            ).fetchone()
            self.assertEqual(mid[0], 3)
            self.assertEqual(mid[1], "cheap_filter")
            conn.close()

    def test_same_day_runs_keep_their_figures(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "history.sqlite")
            morning = pipeline.run(
                [_filing(ACC_CENTER, "8-K", "Financing for data center construction.", ["1.01"])],
                db,
                documents={ACC_CENTER: "The facility funds data center construction of US$180 million."},
                started_at="2026-03-04T08:00:00Z",
            )
            afternoon = pipeline.run(
                [_filing(ACC_CAPEX, "424B2", "Proceeds fund capital expenditures of US$40 million at the plant.")],
                db,
                documents={ACC_CAPEX: "Proceeds fund capital expenditures of US$40 million at the plant."},
                started_at="2026-03-04T15:30:45Z",
            )
            self.assertEqual(morning["run_id"], "2026-03-04-080000-debt-raise")
            self.assertEqual(afternoon["run_id"], "2026-03-04-153045-debt-raise")
            conn = sqlite3.connect(db)
            first_value = conn.execute(
                "SELECT value FROM figures WHERE run_id = ?", (morning["run_id"],)
            ).fetchone()[0]
            second_value = conn.execute(
                "SELECT value FROM figures WHERE run_id = ?", (afternoon["run_id"],)
            ).fetchone()[0]
            self.assertEqual(first_value, 180_000_000)
            self.assertEqual(second_value, 40_000_000)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM debt_scout_runs").fetchone()[0], 2)
            conn.close()

    def test_fetcher_uses_index_text_and_pages_without_downloading(self):
        page1 = json.loads((FIX / "efts_page1.json").read_text(encoding="utf-8"))
        page2 = json.loads((FIX / "efts_page2.json").read_text(encoding="utf-8"))
        calls = []

        class Fake:
            def json(self, url):
                calls.append(url)
                if "from=0" in url:
                    return page1
                if "from=1" in url:
                    return page2
                raise AssertionError(url)

            def get(self, url, accept=""):
                raise AssertionError("full document during collect")

        officer = {
            "_source": {
                "adsh": ACC_OFFICER,
                "ciks": ["999001"],
                "form": "8-K",
                "file_date": "2026-03-03",
                "items": "5.02",
                "file_description": "Departure of a director",
                "display_names": ["Northwind Grid  (NWT)  (CIK 0000999001)"],
            }
        }
        page2["hits"]["hits"].append(officer)
        page2["hits"]["total"]["value"] = 3
        page1["hits"]["total"]["value"] = 3
        filings = fetcher.collect("2026-03-01", "2026-03-31", client=Fake())
        accessions = [row["accession"] for row in filings]
        self.assertEqual(accessions, [ACC_GENERIC, ACC_THIN])
        self.assertEqual(filings[0]["ticker"], "NWT")
        self.assertEqual(filings[0]["items"], ["1.01", "9.01"])
        self.assertIn("general corporate purposes", filings[0]["summary"])
        self.assertTrue(filings[0]["document_url"].startswith("https://www.sec.gov/"))
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(url.startswith("https://efts.sec.gov/") for url in calls))
        self.assertNotIn("q=", calls[0])
        self.assertNotIn("8-K%2FA", calls[0])
        self.assertNotIn("8-K/A", calls[0])

    def test_full_document_fetch_stays_on_sec(self):
        index = json.loads((FIX / "sec_index.json").read_text(encoding="utf-8"))
        body = (FIX / "full_capex.txt").read_text(encoding="utf-8")
        calls = []

        class Fake:
            def json(self, url):
                calls.append(url)
                return index

            def get(self, url, accept=""):
                calls.append(url)
                return body, "text/plain"

        text = fetcher.fetch_primary_text(Fake(), "0000999001", ACC_THIN)
        self.assertIn("capital expenditures", text)
        self.assertTrue(calls[0].endswith("/index.json"))
        self.assertTrue(calls[1].endswith("/d424b5.htm"))
        names = [
            "0000999001-26-000011-index.html",
            "0000999001-26-000011.txt",
            "R1.htm",
            "R12.html",
            "ex99.htm",
            "d424b5.htm",
        ]
        self.assertEqual(fetcher.choose_primary_name(names), "d424b5.htm")
        self.assertTrue(fetcher.summary_is_form_label("424B5", "424B5"))
        self.assertTrue(fetcher.summary_is_form_label("FORM 8-K", "8-K"))
        self.assertFalse(fetcher.summary_is_form_label("Prospectus supplement", "424B5"))
        self.assertTrue(all("sec.gov" in url for url in calls))
        with self.assertRaises(fetcher.EndpointError):
            fetcher._require_sec("https://example.com/file")

    def test_default_sec_client_refuses_before_any_socket(self):
        from shared.nethttp import EndpointError

        os.environ.pop("SEC_CONTACT_EMAIL", None)
        with self.assertRaises(EndpointError):
            fetcher.collect("2026-03-01", "2026-03-31")

    def test_first_amount_keeps_the_lead_currency(self):
        text = (
            "Scentre Group priced US$750 million (A$1,079 million) of senior notes. "
            "Proceeds of the issue will be used for general corporate purposes, "
            "including to repay existing indebtedness."
        )
        amount = first_amount(text)
        self.assertEqual(amount["value"], 750_000_000)
        self.assertEqual(amount["currency"], "USD")
        self.assertEqual(amount["text"], "US$750 million")
        prospectus = (
            "The notes will be issued in minimum denominations of $2,000 and integral multiples of $1,000. "
            "The common stock has a par value of $0.01 per share and the preferred stock has a par value of $0.0001. "
            "The registration fee is $153,500. "
            "We are offering $500,000,000 aggregate principal amount of the notes. "
            "The offering is 12 million shares."
        )
        offering = first_amount(prospectus)
        self.assertEqual(offering["value"], 500_000_000)
        self.assertEqual(offering["text"], "$500,000,000")
        self.assertIsNone(first_amount("minimum denominations of $2,000 and integral multiples of $1,000"))
        self.assertIsNone(first_amount("par value of $0.01 and a registration fee of $153,500"))
        scored = score_text(text, self.rules)
        self.assertEqual(scored.score, 1)
        self.assertIn("generic", scored.matched)
        self.assertIn("refinance", scored.matched)

    def test_asx_headlines_stay_on_the_list_until_the_full_read(self):
        payload = {
            "data": {
                "displayName": "SCENTRE GROUP",
                "items": [
                    {
                        "date": "2026-10-08T22:27:29.000Z",
                        "headline": "Scentre Group Prices US$750 Million of Senior Notes",
                        "documentKey": "2924-example",
                    },
                    {
                        "date": "2026-10-09T00:30:24.000Z",
                        "headline": "Notification regarding unquoted securities - SCG",
                        "documentKey": "2924-other",
                    },
                ],
            }
        }
        calls = []

        class Fake:
            def json(self, url):
                calls.append(url)
                return payload

            def get(self, url, accept=""):
                raise AssertionError("pdf during list")

        filings = fetcher.collect_asx([{"ticker": "SCG", "name": "Scentre Group"}], client=Fake())
        self.assertEqual(len(filings), 1)
        self.assertEqual(filings[0]["ticker"], "SCG.AX")
        self.assertEqual(filings[0]["form"], "ASX")
        self.assertIn("US$750 Million", filings[0]["summary"])
        self.assertTrue(filings[0]["document_url"].startswith("https://asx.api.markitdigital.com/"))
        self.assertTrue(calls[0].startswith("https://asx.api.markitdigital.com/asx-research/1.0/companies/scg/"))
        with self.assertRaises(fetcher.EndpointError):
            fetcher._require_asx("https://example.com/file")

    def test_migration_is_idempotent_and_histogram_matches_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "history.sqlite")
            pipeline.run(
                [_filing(ACC_GENERIC, "8-K", "general corporate purposes", ["1.01"])],
                db,
                started_at="2026-05-01T00:00:00Z",
            )
            from store import open_db

            conn = open_db(db)
            applied = conn.execute("SELECT name FROM schema_migrations").fetchall()
            self.assertEqual(
                sorted(row["name"] for row in applied),
                ["001_history.sql", "shared_schema.sql"],
            )
            stored = json.loads(
                conn.execute("SELECT score_histogram FROM debt_scout_runs").fetchone()["score_histogram"]
            )
            scores = [
                row["score"]
                for row in conn.execute("SELECT score FROM debt_scout_filing_scores")
            ]
            self.assertEqual(stored, histogram(scores))
            conn.close()
            again = open_db(db)
            applied = again.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
            self.assertEqual(applied, 2)
            again.close()

    def test_same_day_filings_keep_distinct_periods_and_form_name_uses_first_page(self):
        same_day = "2026-03-01"
        first = _filing(ACC_GENERIC, "8-K", "8-K", ["1.01"])
        second = _filing(ACC_CENTER, "8-K", "8-K", ["2.03"])
        first["filed"] = same_day
        second["filed"] = same_day
        documents = {
            ACC_GENERIC: "Proceeds will be used for general corporate purposes. The facility is $40 million.",
            ACC_CENTER: "The facility funds data center construction of US$180 million.",
        }
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "history.sqlite")
            summary = pipeline.run(
                [first, second],
                db,
                documents=documents,
                started_at="2026-03-04T12:00:00Z",
            )
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT accession, period, stage_reached, score FROM debt_scout_filing_scores s "
                "JOIN figures f ON f.run_id = s.run_id AND f.ticker = s.ticker "
                "WHERE s.run_id = ?",
                (summary["run_id"],),
            ).fetchall()
            periods = {
                row["period"]
                for row in conn.execute("SELECT period FROM figures")
            }
            conn.close()
            self.assertEqual(periods, {f"{same_day} {ACC_GENERIC}", f"{same_day} {ACC_CENTER}"})
            self.assertEqual(len(periods), 2)
            by_acc = {row["accession"]: row for row in rows}
            self.assertEqual(by_acc[ACC_GENERIC]["stage_reached"], "cheap_filter")
            self.assertEqual(by_acc[ACC_GENERIC]["score"], 0)

    def test_index_description_settles_without_a_primary_download(self):
        calls = []

        class Fake:
            def json(self, url):
                calls.append(("json", url))
                return {
                    "directory": {
                        "item": [
                            {"name": "d8k.htm", "description": "Proceeds for general corporate purposes"},
                            {"name": "0000999001-26-000010-index.html", "description": "8-K"},
                        ]
                    }
                }

            def get(self, url, accept=""):
                calls.append(("get", url))
                raise AssertionError(url)

        filing = _filing(ACC_GENERIC, "8-K", "8-K", ["1.01"])
        with tempfile.TemporaryDirectory() as tmp:
            pipeline.run([filing], str(Path(tmp) / "history.sqlite"), client=Fake(), started_at="2026-03-04T00:00:00Z")
        self.assertTrue(calls)
        self.assertTrue(all(kind == "json" for kind, _url in calls))

    def test_live_fixture_stores_a_figure_and_empty_run_exits_nonzero(self):
        import live

        previous = os.environ.pop("SEC_CONTACT_EMAIL", None)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                db = str(Path(tmp) / "history.sqlite")
                code = live.main(
                    ["--db", db, "--sec-fixture", str(FIX / "sec_check.json"), "--no-asx"]
                )
                self.assertEqual(code, 0)
                conn = sqlite3.connect(db)
                row = conn.execute(
                    "SELECT ticker, value, currency FROM figures"
                ).fetchone()
                conn.close()
                self.assertEqual(row[0], "NWT")
                self.assertEqual(row[1], 180_000_000)
                self.assertEqual(row[2], "USD")
                empty = live.main(["--db", str(Path(tmp) / "empty.sqlite"), "--no-asx"])
                self.assertEqual(empty, 1)
        finally:
            if previous is not None:
                os.environ["SEC_CONTACT_EMAIL"] = previous


if __name__ == "__main__":
    unittest.main()
