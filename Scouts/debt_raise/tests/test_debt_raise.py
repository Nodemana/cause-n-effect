"""Fixture tests for the debt-raise use-of-proceeds gate. No network."""

import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import urllib.parse
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path

SCOUT = Path(__file__).resolve().parents[1]
REPO = SCOUT.parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(SCOUT))

from Scouts.debt_raise import fetcher  # noqa: E402
from Scouts.debt_raise import report  # noqa: E402
from Scouts.debt_raise import scout_pipeline as pipeline  # noqa: E402
from Scouts.debt_raise.score import (  # noqa: E402
    deal_amount,
    first_amount,
    histogram,
    load_config,
    needs_full_document,
    pick_amount,
    score_text,
)

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
                [_filing(ACC_CAPEX, "424B2", "Proceeds fund capital expenditures of US$80 million at the plant.")],
                db,
                documents={ACC_CAPEX: "Proceeds fund capital expenditures of US$80 million at the plant."},
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
            self.assertEqual(second_value, 80_000_000)
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
        self.assertEqual(deal_amount(text)["value"], 750_000_000)
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

    def test_deal_amount_prefers_principal_and_sums_tranches(self):
        amzn = (
            "A covenant refers to an aggregate principal amount of $10 million. "
            "The Company is offering $5,000,000,000 aggregate principal amount of "
            "4.650% Notes due 2032."
        )
        oracle = (
            "The covenant schedule lists $25 million. "
            "The offering of senior notes has an aggregate principal amount of $8,000,000,000."
        )
        tranches = (
            "The Company is offering $1,000,000,000 aggregate principal amount of "
            "4.100% Notes due 2030 and $750,000,000 aggregate principal amount of "
            "4.400% Notes due 2035."
        )
        stated = (
            tranches
            + " The total aggregate principal amount of the notes offered is $1,750,000,000."
        )
        converted = (
            "The aggregate principal amount is US$750 million (A$1,079 million) of senior notes."
        )
        self.assertEqual(deal_amount(amzn)["value"], 5_000_000_000)
        self.assertEqual(deal_amount(oracle)["value"], 8_000_000_000)
        summed = deal_amount(tranches)
        self.assertEqual(summed["value"], 1_750_000_000)
        self.assertEqual(summed["currency"], "USD")
        self.assertIn("+", summed["text"])
        self.assertEqual(deal_amount(stated)["value"], 1_750_000_000)
        self.assertEqual(deal_amount(stated)["text"], "$1,750,000,000")
        self.assertEqual(deal_amount(converted)["value"], 750_000_000)
        self.assertEqual(deal_amount(converted)["currency"], "USD")
        once = (
            "The Company is offering $10,250,000,000 aggregate principal amount of "
            "4.800% Notes due 2031 and $15,000,000,000 aggregate principal amount of "
            "5.150% Notes due 2036."
        )
        repeated = f"{once} Cover page. {once}"
        self.assertEqual(deal_amount(repeated)["value"], 25_250_000_000)
        totaled = repeated + " The total aggregate principal amount of the notes is $25,250,000,000."
        self.assertEqual(deal_amount(totaled)["value"], 25_250_000_000)
        self.assertEqual(deal_amount(totaled)["text"], "$25,250,000,000")
        self.assertEqual(self.config["min_deal_usd"], 50_000_000)
        for raw, expected in (("$1 million", 1_000_000), ("$2,000,000", 2_000_000), ("$3.5 million", 3_500_000)):
            note = f"Structured note. The aggregate principal amount of the notes is {raw}."
            self.assertEqual(deal_amount(note)["value"], expected)
            self.assertLess(deal_amount(note)["value"], 50_000_000)

    def test_small_structured_notes_are_logged_not_stored(self):
        small = _filing(
            "0000999001-26-000424",
            "424B2",
            "Structured note. The aggregate principal amount of the notes is $2,500,000.",
            ticker="AMZN",
        )
        large = _filing(
            ACC_CENTER,
            "8-K",
            "The facility funds data center construction of US$180 million.",
            ["1.01"],
        )
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "history.sqlite")
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                pipeline.run(
                    [small, large],
                    db,
                    documents={
                        small["accession"]: small["summary"],
                        large["accession"]: large["summary"],
                    },
                    started_at="2026-06-01T00:00:00Z",
                )
            logged = stderr.getvalue()
            self.assertIn("dropped deal below minimum", logged)
            self.assertIn("ticker=AMZN", logged)
            self.assertIn("form=424B2", logged)
            self.assertIn("2500000", logged)
            conn = sqlite3.connect(db)
            values = [row[0] for row in conn.execute("SELECT value FROM figures")]
            forms = [
                row[0]
                for row in conn.execute("SELECT form FROM debt_scout_filing_scores ORDER BY form")
            ]
            conn.close()
            self.assertEqual(values, [180_000_000])
            self.assertIn("424B2", forms)
            self.assertIn("8-K", forms)

        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "history.sqlite")
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                pipeline.run(
                    [small],
                    db,
                    documents={small["accession"]: small["summary"]},
                    min_deal=1_000_000,
                    started_at="2026-06-02T00:00:00Z",
                )
            conn = sqlite3.connect(db)
            stored = conn.execute("SELECT value FROM figures").fetchone()[0]
            conn.close()
            self.assertEqual(stored, 2_500_000)
            self.assertNotIn("dropped deal below minimum", stderr.getvalue())

    def test_same_issuer_deal_is_not_stored_twice_within_a_few_days(self):
        body = (
            "The Company is offering $10,250,000,000 aggregate principal amount of "
            "4.800% Notes due 2031 and $15,000,000,000 aggregate principal amount of "
            "5.150% Notes due 2036. "
            "The total aggregate principal amount of the notes is $25,250,000,000."
        )
        other = "The facility funds data center construction of US$90 million."
        first = _filing("0001193125-26-035603", "8-K", body, ["2.03"], ticker="ORCL")
        second = _filing("0001193125-26-037840", "424B5", body, ticker="ORCL")
        separate = _filing("0001193125-26-040000", "8-K", other, ["1.01"], ticker="ORCL")
        first["filed"] = "2026-02-02"
        second["filed"] = "2026-02-06"
        separate["filed"] = "2026-02-04"
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "history.sqlite")
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                pipeline.run(
                    [first, second, separate],
                    db,
                    documents={
                        first["accession"]: body,
                        second["accession"]: body,
                        separate["accession"]: other,
                    },
                    started_at="2026-02-06T00:00:00Z",
                )
            logged = stderr.getvalue()
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            figures = conn.execute(
                "SELECT value, period, announced_on FROM figures ORDER BY value"
            ).fetchall()
            scores = conn.execute("SELECT COUNT(*) FROM debt_scout_filing_scores").fetchone()[0]
            conn.close()
        self.assertIn("dropped duplicate deal", logged)
        self.assertIn("ticker=ORCL", logged)
        self.assertIn("0001193125-26-035603", logged)
        self.assertIn("kept_accession=0001193125-26-037840", logged)
        self.assertEqual(scores, 3)
        self.assertEqual(sorted(row["value"] for row in figures), [90_000_000, 25_250_000_000])
        notes = next(row for row in figures if row["value"] == 25_250_000_000)
        self.assertIn("0001193125-26-037840", notes["period"])
        self.assertEqual(notes["announced_on"], "2026-02-06")

    def test_orcl_prospectus_keeps_the_cover_total_not_the_second_listing(self):
        prospectus = (FIX / "orcl_424b2_035603.txt").read_text(encoding="utf-8")
        certificate = (FIX / "orcl_8k_037840.txt").read_text(encoding="utf-8")
        self.assertIn("Issuance of Additional Notes", prospectus)
        self.assertIn("$2,250,000,000 for the 2046 Fixed Rate Notes", prospectus)
        cover = deal_amount(prospectus)
        self.assertEqual(cover["value"], 25_000_000_000)
        self.assertEqual(cover["text"], "$25,000,000,000")
        self.assertNotEqual(cover["value"], 42_500_000_000)
        self.assertNotIn("+", cover["text"])
        officers = deal_amount(certificate)
        self.assertEqual(officers["value"], 25_000_000_000)
        self.assertEqual(officers["currency"], "USD")

    def test_orcl_8k_and_424b_within_a_week_store_one_deal(self):
        prospectus = (FIX / "orcl_424b2_035603.txt").read_text(encoding="utf-8")
        certificate = (FIX / "orcl_8k_037840.txt").read_text(encoding="utf-8")
        facility = "The facility funds data center construction of US$90 million."
        first = _filing("0001193125-26-035603", "424B2", prospectus, ticker="ORCL")
        second = _filing("0001193125-26-037840", "8-K", certificate, ["2.03"], ticker="ORCL")
        separate = _filing("0001193125-26-040000", "8-K", facility, ["1.01"], ticker="ORCL")
        first["filed"] = "2026-02-03"
        second["filed"] = "2026-02-04"
        separate["filed"] = "2026-02-05"
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "history.sqlite")
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                pipeline.run(
                    [first, second, separate],
                    db,
                    documents={
                        first["accession"]: prospectus,
                        second["accession"]: certificate,
                        separate["accession"]: facility,
                    },
                    started_at="2026-02-05T00:00:00Z",
                )
            logged = stderr.getvalue()
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            figures = conn.execute(
                "SELECT value, period, announced_on FROM figures ORDER BY value"
            ).fetchall()
            conn.close()
        self.assertIn("dropped duplicate deal", logged)
        self.assertIn("0001193125-26-035603", logged)
        self.assertIn("kept_accession=0001193125-26-037840", logged)
        self.assertEqual(sorted(row["value"] for row in figures), [90_000_000, 25_000_000_000])
        notes = next(row for row in figures if row["value"] == 25_000_000_000)
        self.assertIn("0001193125-26-037840", notes["period"])
        self.assertEqual(notes["announced_on"], "2026-02-04")

    def test_near_amounts_or_an_8k_and_424b_are_one_deal(self):
        def figure(value, form, filed, accession, figure_type="bond_amount"):
            return {
                "ticker": "ORCL",
                "currency": "USD",
                "figure_type": figure_type,
                "value": value,
                "form": form,
                "accession": accession,
                "announced_on": filed,
            }

        doubled = [
            figure(42_500_000_000, "424B2", "2026-02-03", "0001193125-26-035603"),
            figure(25_000_000_000, "8-K", "2026-02-04", "0001193125-26-037840"),
        ]
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            kept = pipeline._dedupe_issuer_deals(doubled)
        self.assertEqual([row["accession"] for row in kept], ["0001193125-26-037840"])
        self.assertIn("0001193125-26-035603", stderr.getvalue())

        close = [
            figure(25_000_000_000, "8-K", "2026-02-03", "0001193125-26-000001"),
            figure(27_000_000_000, "8-K", "2026-02-06", "0001193125-26-000002"),
        ]
        with redirect_stderr(io.StringIO()):
            kept = pipeline._dedupe_issuer_deals(close)
        self.assertEqual([row["value"] for row in kept], [27_000_000_000])

        apart = [
            figure(25_000_000_000, "8-K", "2026-02-03", "0001193125-26-000003"),
            figure(40_000_000_000, "8-K", "2026-02-06", "0001193125-26-000004"),
        ]
        kept = pipeline._dedupe_issuer_deals(apart)
        self.assertEqual(sorted(row["value"] for row in kept), [25_000_000_000, 40_000_000_000])

        late = [
            figure(25_000_000_000, "424B2", "2026-02-03", "0001193125-26-035603"),
            figure(25_000_000_000, "8-K", "2026-02-11", "0001193125-26-037840"),
        ]
        kept = pipeline._dedupe_issuer_deals(late)
        self.assertEqual(len(kept), 2)

        mixed = [
            figure(25_000_000_000, "8-K", "2026-02-04", "0001193125-26-037840"),
            figure(90_000_000, "8-K", "2026-02-05", "0001193125-26-040000", figure_type="facility_amount"),
        ]
        kept = pipeline._dedupe_issuer_deals(mixed)
        self.assertEqual(sorted(row["figure_type"] for row in kept), ["bond_amount", "facility_amount"])

    def test_amount_fixtures_pick_the_deal_and_send_the_rest_to_review(self):
        cases = [
            ("amount_denomination.txt", "DEN", None),
            ("amount_par.txt", "PAR", None),
            ("amount_fee.txt", "FEE", None),
            ("amount_aggregate.txt", "AGG", 750_000_000),
            ("amount_offering.txt", "OFF", 500_000_000),
            ("amount_cover.txt", "COV", 1_250_000_000),
        ]
        filings = []
        documents = {}
        for index, (name, ticker, expected) in enumerate(cases, start=1):
            text = (FIX / name).read_text(encoding="utf-8")
            amount, needs_review = pick_amount(text)
            if expected is None:
                self.assertIsNone(amount, name)
                self.assertIsNone(first_amount(text), name)
                self.assertTrue(needs_review, name)
            else:
                self.assertEqual(amount["value"], expected, name)
                self.assertEqual(amount["currency"], "USD", name)
                self.assertNotEqual(amount["text"], "$1,000", name)
                self.assertFalse(needs_review, name)
            accession = f"0000999001-26-0001{index:02d}"
            filings.append(_filing(accession, "424B5", "424B5", ticker=ticker))
            documents[accession] = text

        tiny, tiny_review = pick_amount("The aggregate principal amount is $750,000.")
        self.assertIsNone(tiny)
        self.assertTrue(tiny_review)
        mixed, mixed_review = pick_amount("A $200 million term loan and a $50 million revolver.")
        self.assertIsNone(mixed)
        self.assertTrue(mixed_review)

        class Fake:
            def json(self, url):
                raise AssertionError(url)

            def get(self, url, accept=""):
                raise AssertionError(url)

        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "history.sqlite")
            pipeline.run(
                filings,
                db,
                client=Fake(),
                documents=documents,
                started_at="2026-06-01T00:00:00Z",
            )
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            figures = {
                row["ticker"]: row
                for row in conn.execute(
                    "SELECT ticker, figure_type, value, value_text, currency, verified FROM figures"
                )
            }
            flags = {
                row["ticker"]: json.loads(row["flags"])
                for row in conn.execute("SELECT ticker, flags FROM debt_scout_filing_scores")
            }
            conn.close()

        for ticker in ("DEN", "PAR", "FEE"):
            self.assertNotIn(ticker, figures)
            self.assertIn("review", flags[ticker])
        self.assertEqual(figures["AGG"]["figure_type"], "bond_amount")
        self.assertEqual(figures["AGG"]["value"], 750_000_000)
        self.assertEqual(figures["AGG"]["value_text"], "$750,000,000")
        self.assertEqual(figures["AGG"]["currency"], "USD")
        self.assertEqual(figures["AGG"]["verified"], 1)
        self.assertNotIn("review", flags["AGG"])
        self.assertEqual(figures["OFF"]["value"], 500_000_000)
        self.assertEqual(figures["OFF"]["value_text"], "$500 million")
        self.assertEqual(figures["OFF"]["figure_type"], "bond_amount")
        self.assertNotIn("review", flags["OFF"])
        self.assertEqual(figures["COV"]["value"], 1_250_000_000)
        self.assertEqual(figures["COV"]["value_text"], "$1,250,000,000")
        self.assertEqual(figures["COV"]["figure_type"], "bond_amount")
        self.assertNotIn("review", flags["COV"])

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
            ACC_GENERIC: "Proceeds will be used for general corporate purposes. The facility is $80 million.",
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

    def test_ticker_search_filters_by_cik_and_fills_missing_424b_tickers(self):
        tickers = json.loads((FIX / "company_tickers.json").read_text(encoding="utf-8"))
        page = json.loads((FIX / "efts_by_cik.json").read_text(encoding="utf-8"))
        calls = []

        class Fake:
            def json(self, url):
                calls.append(url)
                if url.startswith(fetcher.TICKERS_URL):
                    return tickers
                if "efts.sec.gov" in url:
                    if "from=0" not in url:
                        raise AssertionError(url)
                    return page
                raise AssertionError(url)

            def get(self, url, accept=""):
                raise AssertionError("full document during collect")

        filings, missing = fetcher.collect_window(
            "2025-10-12",
            "2026-10-12",
            tickers=["amzn", "ORCL", "MSFT", "NOPE"],
            client=Fake(),
            limit=50,
        )
        self.assertEqual(missing, ["NOPE"])
        self.assertEqual(
            [row["accession"] for row in filings],
            [
                "0001018724-25-900001",
                "0001341439-25-900002",
                "0000789019-25-900003",
                "0001018724-25-900004",
                "0001341439-25-900005",
                "0000789019-25-900006",
            ],
        )
        by_acc = {row["accession"]: row for row in filings}
        self.assertEqual(by_acc["0001018724-25-900001"]["ticker"], "AMZN")
        self.assertEqual(by_acc["0001018724-25-900001"]["cik"], "0001018724")
        self.assertNotIn("AMZN", by_acc["0001018724-25-900001"]["summary"])
        self.assertEqual(by_acc["0001341439-25-900002"]["ticker"], "ORCL")
        self.assertEqual(by_acc["0000789019-25-900003"]["ticker"], "MSFT")
        self.assertEqual(by_acc["0000789019-25-900003"]["form"], "424B5")
        self.assertEqual(by_acc["0001341439-25-900005"]["ticker"], "ORCL")
        self.assertEqual(by_acc["0000789019-25-900006"]["items"], ["2.03"])
        prospectus = [
            hit
            for hit in page["hits"]["hits"]
            if str(hit["_source"]["form"]).startswith("424B2")
        ]
        self.assertEqual(len(prospectus), 5)
        missing_display = [
            hit
            for hit in prospectus
            if fetcher.ticker_from_display(hit["_source"]["display_names"]) is None
        ]
        self.assertEqual(len(missing_display), 4)

        self.assertTrue(calls[0].startswith(fetcher.TICKERS_URL))
        efts_urls = [url for url in calls if "efts.sec.gov" in url]
        self.assertEqual(len(efts_urls), 1)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(efts_urls[0]).query)
        self.assertNotIn("q", query)
        self.assertNotIn("*", efts_urls[0])
        self.assertNotIn("AMZN", efts_urls[0])
        self.assertEqual(query["startdt"], ["2025-10-12"])
        self.assertEqual(query["enddt"], ["2026-10-12"])
        self.assertEqual(
            query["ciks"],
            ["0001018724,0001341439,0000789019"],
        )
        forms = query["forms"][0].split(",")
        self.assertIn("8-K", forms)
        self.assertIn("424B2", forms)
        self.assertIn("424B5", forms)
        self.assertNotIn("8-K/A", forms)
        self.assertNotIn("8-K/A", fetcher.FORMS)

    def test_cik_search_stops_at_limit_without_another_page(self):
        tickers = json.loads((FIX / "company_tickers.json").read_text(encoding="utf-8"))
        page = json.loads((FIX / "efts_by_cik.json").read_text(encoding="utf-8"))
        page["hits"]["total"]["value"] = 500
        calls = []

        class Fake:
            def json(self, url):
                calls.append(url)
                if url.startswith(fetcher.TICKERS_URL):
                    return tickers
                if "efts.sec.gov" in url:
                    if "from=0" not in url:
                        raise AssertionError(url)
                    return page
                raise AssertionError(url)

            def get(self, url, accept=""):
                raise AssertionError("full document during collect")

        filings, missing = fetcher.collect_window(
            "2025-10-12",
            "2026-10-12",
            tickers=["AMZN", "ORCL", "MSFT"],
            client=Fake(),
            limit=2,
        )
        self.assertEqual(missing, [])
        self.assertEqual(
            [row["ticker"] for row in filings],
            ["AMZN", "ORCL"],
        )
        self.assertEqual(len([url for url in calls if "efts.sec.gov" in url]), 1)

    def test_unknown_ticker_does_not_search_edgar(self):
        tickers = json.loads((FIX / "company_tickers.json").read_text(encoding="utf-8"))
        calls = []

        class Fake:
            def json(self, url):
                calls.append(url)
                if url.startswith(fetcher.TICKERS_URL):
                    return tickers
                raise AssertionError(url)

            def get(self, url, accept=""):
                raise AssertionError(url)

        filings, missing = fetcher.collect_window(
            "2025-10-12",
            "2026-10-12",
            tickers=["NOPE"],
            client=Fake(),
        )
        self.assertEqual(filings, [])
        self.assertEqual(missing, ["NOPE"])
        self.assertEqual(calls, [fetcher.TICKERS_URL])

    def test_live_forwards_tickers_since_and_limit_without_a_socket(self):
        import live

        seen = {}

        def collect_window(start, end, tickers=None, client=None, limit=None):
            seen.update(start=start, end=end, tickers=tickers, client=client, limit=limit)
            return [], []

        original_collect = fetcher.collect_window
        original_block = live.sec_block_message
        fetcher.collect_window = collect_window
        live.sec_block_message = lambda: None
        previous = os.environ.pop("SEC_CONTACT_EMAIL", None)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                code = live.main(
                    [
                        "--db",
                        str(Path(tmp) / "history.sqlite"),
                        "--tickers",
                        "AMZN,ORCL,MSFT",
                        "--since",
                        "2025-10-12",
                        "--until",
                        "2026-10-08",
                        "--limit",
                        "40",
                        "--no-asx",
                    ]
                )
                self.assertEqual(code, 1)
                self.assertEqual(seen["start"], "2025-10-12")
                self.assertEqual(seen["end"], "2026-10-08")
                self.assertEqual(seen["tickers"], ["AMZN", "ORCL", "MSFT"])
                self.assertEqual(seen["limit"], 40)
                self.assertIsNone(seen["client"])
                seen.clear()
                live.main(["--db", str(Path(tmp) / "default.sqlite"), "--no-asx"])
                self.assertEqual(seen["limit"], live.DEFAULT_LIMIT)
                self.assertEqual(seen["limit"], 50)
                self.assertIsNone(seen["tickers"])
                start = date.fromisoformat(seen["start"])
                end = date.fromisoformat(seen["end"])
                self.assertEqual((end - start).days, 3)
        finally:
            fetcher.collect_window = original_collect
            live.sec_block_message = original_block
            if previous is not None:
                os.environ["SEC_CONTACT_EMAIL"] = previous

    def test_live_tickers_without_contact_sends_nothing(self):
        import live

        previous = os.environ.pop("SEC_CONTACT_EMAIL", None)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    code = live.main(
                        [
                            "--db",
                            str(Path(tmp) / "history.sqlite"),
                            "--tickers",
                            "AMZN,ORCL,MSFT",
                            "--since",
                            "2025-10-12",
                            "--no-asx",
                        ]
                    )
                self.assertEqual(code, 1)
                self.assertIn("SEC_CONTACT_EMAIL is missing", buffer.getvalue())
                self.assertIn("No filings to score.", buffer.getvalue())
        finally:
            if previous is not None:
                os.environ["SEC_CONTACT_EMAIL"] = previous

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

    def test_help_exits_zero_without_network(self):
        import subprocess
        import sys

        env = {key: value for key, value in os.environ.items() if key != "SEC_CONTACT_EMAIL"}
        root = str(Path(__file__).resolve().parents[3])
        for flag in ("--help", "-h"):
            completed = subprocess.run(
                [sys.executable, "-m", "Scouts.debt_raise.live", flag],
                cwd=root,
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("usage", completed.stdout.lower())


if __name__ == "__main__":
    unittest.main()
