"""Fixture tests for the merged orders-backlog scout. No network."""

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

from Scouts.orders_backlog import fetcher  # noqa: E402
from Scouts.orders_backlog import live  # noqa: E402
from Scouts.orders_backlog import parser  # noqa: E402
from Scouts.orders_backlog import scout_pipeline as pipeline  # noqa: E402
from shared.store import dedupe_figures  # noqa: E402
from Scouts.orders_backlog.tags import CUSTOMER_SECTORS, PROXY_TAGS, SUPPLIER_EQUIPMENT  # noqa: E402

FIX = SCOUT / "fixtures"
AMAT = {
    "ticker": "AMAT",
    "name": "Applied Materials Inc",
    "cik": "0000006951",
    "sector": "semiconductor_equipment",
    "backlog_tags": ["RevenueRemainingPerformanceObligation"],
    "stale_tags": ["BacklogOfOrders"],
}
SUPPLIER = {
    "ticker": "NWT",
    "name": "Northwind Turbines",
    "cik": "0000999001",
    "market": "SEC",
    "equipment": ["turbines", "transformers", "switchgear"],
    "intake_tags": ["OrdersReceived"],
    "ir_urls": [],
}


class OrdersBacklogTests(unittest.TestCase):
    def setUp(self):
        self._urlopen = urllib.request.urlopen
        urllib.request.urlopen = self._refuse

    def tearDown(self):
        urllib.request.urlopen = self._urlopen

    @staticmethod
    def _refuse(*_args, **_kwargs):
        raise AssertionError("live network")

    def test_universe_files_are_editable_and_on_scope(self):
        customers = fetcher.load_customers()
        suppliers = fetcher.load_suppliers()
        tickers = {row["ticker"] for row in customers}
        self.assertTrue({"AMAT", "LRCX", "KLAC", "LMT"} <= tickers)
        for row in customers:
            self.assertIn(row["sector"], CUSTOMER_SECTORS)
        for row in suppliers:
            self.assertTrue(set(row["equipment"]) <= set(SUPPLIER_EQUIPMENT))
            self.assertNotIn(row["ticker"], tickers)

    def test_company_facts_backlog_ignores_deferred_revenue(self):
        payload = json.loads((FIX / "amat_facts.json").read_text(encoding="utf-8"))
        self.assertIn("DeferredRevenue", PROXY_TAGS)
        rows = parser.parse_company_facts(payload, AMAT, "customer_backlog")
        included = [row for row in rows if not row["flags"]]
        review = [row for row in rows if row["flags"]]
        self.assertEqual(len(included), 1)
        self.assertEqual(included[0]["source_side"], "customer_backlog")
        self.assertEqual(included[0]["value"], 12400000000)
        self.assertEqual(included[0]["currency"], "USD")
        self.assertEqual(included[0]["equipment_or_period"], "2026-04-26")
        self.assertEqual(included[0]["timing"], "Q2 FY2026")
        self.assertEqual(included[0]["filing_date"], "2026-05-20")
        self.assertIn("0000006951-26-000010", included[0]["source_url"])
        self.assertIn("accession:0000006951-26-000010", included[0]["notes"])
        self.assertEqual(included[0]["notes"][0], "RevenueRemainingPerformanceObligation")
        self.assertEqual(included[0]["counterparty"], None)
        self.assertTrue(all(row["value"] != 3100000000 for row in rows))
        self.assertEqual(review[0]["flags"], ["stale_tag", "tag_switch"])
        self.assertEqual(review[0]["value"], 9000000000)
        periods = {row["equipment_or_period"] for row in rows}
        self.assertNotIn("2018-04-29", periods)
        self.assertNotIn(2100000000, {row["value"] for row in rows})

    def test_live_company_facts_log_the_accession(self):
        previous = os.environ.pop("SEC_CONTACT_EMAIL", None)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                db = str(Path(tmp) / "history.sqlite")
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    code = live.main(
                        ["--db", db, "--sec-fixture", str(FIX / "amat_facts.json"), "--no-asx"]
                    )
                self.assertEqual(code, 0)
                printed = stdout.getvalue()
        finally:
            if previous is not None:
                os.environ["SEC_CONTACT_EMAIL"] = previous
        self.assertIn("accession=0000006951-26-000010", printed)
        self.assertIn("accession=0000006951-25-000040", printed)
        self.assertNotIn("accession=\n", printed)
        self.assertIn("RevenueRemainingPerformanceObligation", printed)

    def test_release_backlog_and_not_deferred_revenue(self):
        text = (FIX / "exhibit99_backlog.txt").read_text(encoding="utf-8")
        rows = parser.parse_release(text, AMAT, "customer_backlog", "8-K", "https://www.sec.gov/ex99", "2026-05-20")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["value"], 12_400_000_000)
        self.assertEqual(rows[0]["currency"], "USD")
        self.assertEqual(rows[0]["equipment_or_period"], "2026-04-26")
        self.assertEqual(rows[0]["flags"], [])

    def test_transcript_is_not_parsed(self):
        text = "Operator: backlog was $99 billion.\nQuestion-and-Answer session.\n"
        rows = parser.parse_release(text, AMAT, "customer_backlog", "8-K", "https://www.sec.gov/call", "2026-05-20")
        self.assertEqual(rows[0]["flags"], ["skipped_transcript"])
        self.assertIsNone(rows[0]["value"])

    def test_named_supplier_order(self):
        text = (FIX / "exhibit99_order.txt").read_text(encoding="utf-8")
        rows = parser.parse_release(text, SUPPLIER, "supplier_order", "8-K", "https://www.sec.gov/order", "2026-06-02")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["source_side"], "supplier_order")
        self.assertEqual(row["company"], "Northwind Turbines")
        self.assertEqual(row["counterparty"], "Harbour Utilities")
        self.assertEqual(row["equipment_or_period"], "turbines")
        self.assertEqual(row["value"], 180_000_000)
        self.assertEqual(row["currency"], "USD")
        self.assertEqual(row["timing"], "delivery 2027")
        self.assertEqual(row["capacity"], "500 MW")
        self.assertEqual(row["flags"], [])

    def test_unnamed_customer_and_missing_value_are_review(self):
        unnamed = parser.parse_release(
            "Northwind Turbines received an order from an undisclosed customer for switchgear valued at EUR 40 million.",
            SUPPLIER, "supplier_order", "8-K", "https://www.sec.gov/u", "2026-06-03",
        )[0]
        self.assertIn("unnamed_customer", unnamed["flags"])
        self.assertIsNone(unnamed["counterparty"])
        self.assertEqual(unnamed["value"], 40_000_000)
        self.assertEqual(unnamed["currency"], "EUR")
        missing = parser.parse_release(
            "Northwind Turbines announced a transformer order from Copperline Grid for delivery in 2028.",
            SUPPLIER, "supplier_order", "ASX", "https://www.asx.com.au/t", "2026-06-04",
        )[0]
        self.assertIn("missing_value", missing["flags"])
        self.assertEqual(missing["counterparty"], "Copperline Grid")
        self.assertEqual(missing["timing"], "delivery 2028")

    def test_order_intake_is_aggregate(self):
        rows = parser.parse_release(
            "Order intake for the quarter ended June 30, 2026 was $2.4 billion.",
            SUPPLIER, "supplier_order", "8-K", "https://www.sec.gov/intake", "2026-07-20",
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["disclosure"], "order_intake")
        self.assertEqual(rows[0]["value"], 2_400_000_000)
        self.assertEqual(rows[0]["equipment_or_period"], "2026-06-30")
        self.assertEqual(rows[0]["flags"], [])
        self.assertIsNone(rows[0]["counterparty"])

    def test_recent_quarters_are_configurable(self):
        payload = json.loads((FIX / "amat_facts.json").read_text(encoding="utf-8"))
        kept = parser.parse_company_facts(payload, AMAT, "customer_backlog", max_quarters=1)
        ends = {row["equipment_or_period"] for row in kept}
        self.assertEqual(ends, {"2026-04-26"})
        wider = parser.parse_company_facts(payload, AMAT, "customer_backlog", max_quarters=8)
        self.assertIn("2025-10-26", {row["equipment_or_period"] for row in wider})
        self.assertNotIn("2018-04-29", {row["equipment_or_period"] for row in wider})
        everything = parser.parse_company_facts(payload, AMAT, "customer_backlog", max_quarters=0)
        self.assertIn("2018-04-29", {row["equipment_or_period"] for row in everything})

    def test_gev_segment_row_keeps_a_period(self):
        text = (FIX / "exhibit99_gev.txt").read_text(encoding="utf-8")
        gev = dict(SUPPLIER, ticker="GEV", name="GE Vernova Inc")
        rows = parser.parse_release(
            text, gev, "supplier_order", "8-K", "https://www.sec.gov/gev", "2026-07-23"
        )
        self.assertEqual(len(rows), 2)
        intake = next(row for row in rows if row["disclosure"] == "order_intake" and row["value"] == 4_200_000_000)
        self.assertEqual(intake["equipment_or_period"], "2026-06-30")
        self.assertNotIn("|", intake["equipment_or_period"])
        self.assertNotIn("turbines|", intake["equipment_or_period"])
        self.assertIn("segment: turbines", intake["notes"])
        self.assertEqual(intake["currency"], "USD")
        quarter = next(row for row in rows if row["value"] == 18_000_000_000)
        self.assertEqual(quarter["equipment_or_period"], "Q2 2026")
        self.assertNotIn("|", quarter["equipment_or_period"])
        self.assertIn("segment: turbines", quarter["notes"])
        self.assertTrue(all("|" not in (row["equipment_or_period"] or "") for row in rows))

    def test_live_gev_period_is_a_date_and_the_segment_is_separate(self):
        import live

        text = (FIX / "exhibit99_gev_live.txt").read_text(encoding="utf-8")
        gev = dict(SUPPLIER, ticker="GEV", name="GE Vernova Inc", equipment=["turbines"])
        rows = parser.parse_release(
            text, gev, "supplier_order", "8-K", "https://www.sec.gov/gev-live", "2026-07-22"
        )
        figures = live._figures(rows, "2026-07-22")
        self.assertGreaterEqual(len(figures), 2)
        periods = [row["period"] for row in figures]
        self.assertNotIn("turbines|2026-07-22", periods)
        self.assertFalse(any(period.startswith("|") for period in periods))
        dated = next(row for row in figures if row["value"] == 18_000_000_000)
        self.assertEqual(dated["period"], "2026-07-22")
        self.assertEqual(dated["instrument"], "turbines")
        bare = next(row for row in figures if row["value"] == 4_200_000_000)
        self.assertEqual(bare["period"], "2026-07-22")
        self.assertNotEqual(bare["instrument"], "")

    def test_stale_latest_period_is_flagged_in_the_run_output(self):
        import live

        payload = json.loads((FIX / "klac_stale_facts.json").read_text(encoding="utf-8"))
        rows = parser.parse_company_facts(payload, {"ticker": "KLAC", "name": "KLA Corp", "cik": "0000319201"}, "customer_backlog")
        self.assertEqual(rows[0]["equipment_or_period"], "2025-06-30")
        stdout = io.StringIO()
        stale = pipeline.report_stale_latest(rows, "2026-10-12", stream=stdout)
        self.assertEqual([item["ticker"] for item in stale], ["KLAC"])
        self.assertEqual(stale[0]["period"], "2025-06-30")
        self.assertIn("stale latest period ticker=KLAC period=2025-06-30", stdout.getvalue())
        self.assertIn("stale_period", rows[0]["flags"])
        fresh = parser.parse_company_facts(
            json.loads((FIX / "amat_facts.json").read_text(encoding="utf-8")),
            AMAT,
            "customer_backlog",
        )
        self.assertEqual(pipeline.report_stale_latest(fresh, "2026-10-12", stream=io.StringIO()), [])

        previous = os.environ.pop("SEC_CONTACT_EMAIL", None)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                db = str(Path(tmp) / "history.sqlite")
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    code = live.main(
                        ["--db", db, "--sec-fixture", str(FIX / "klac_stale_facts.json"), "--no-asx"]
                    )
                self.assertEqual(code, 0)
                text = stdout.getvalue()
                self.assertIn("stale latest period ticker=KLAC period=2025-06-30", text)
                conn = sqlite3.connect(db)
                note = conn.execute("SELECT notes FROM runs").fetchone()[0]
                conn.close()
                self.assertIn("stale=KLAC:2025-06-30", note)
        finally:
            if previous is not None:
                os.environ["SEC_CONTACT_EMAIL"] = previous

    def test_gev_release_keeps_orders_backlog_and_segment_amounts(self):
        text = (FIX / "exhibit99_gev_2q26.txt").read_text(encoding="utf-8")
        gev = {
            "ticker": "GEV",
            "name": "GE Vernova Inc",
            "cik": "0001996810",
            "equipment": ["turbines", "transformers", "switchgear"],
        }
        rows = parser.parse_release(
            text, gev, "supplier_order", "8-K", "https://www.sec.gov/gev-2q26", "2026-07-22"
        )
        figures = dedupe_figures(pipeline.keep_distinct_amounts(live._figures(rows, "2026-07-22")))
        by_instrument = {row["instrument"]: row for row in figures}
        expected = {
            "orders": 24_200_000_000,
            "orders:Electrification": 6_300_000_000,
            "revenue:Wind": 2_000_000_000,
            "orders:Wind": 1_200_000_000,
            "orders:data center": 5_000_000_000,
        }
        for instrument, value in expected.items():
            self.assertIn(instrument, by_instrument)
            self.assertEqual(by_instrument[instrument]["value"], value)
            self.assertEqual(by_instrument[instrument]["period"], "2026-06-30")
            self.assertNotIn("|", instrument)
        self.assertGreater(len(figures), 1)
        self.assertNotIn("order", by_instrument)

        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "history.sqlite")
            table = pipeline.build_table(releases=[{
                "company": gev,
                "body": text,
                "url": "https://www.sec.gov/gev-2q26",
                "filing_date": "2026-07-22",
                "source_side": "supplier_order",
                "source_type": "8-K",
            }])
            pipeline.store_run(db, table, "2026-07-22T00:00:00Z")
            conn = sqlite3.connect(db)
            stored = {
                row[0]: row[1]
                for row in conn.execute("SELECT instrument, value FROM figures")
            }
            conn.close()
        for instrument, value in expected.items():
            self.assertEqual(stored[instrument], value)

    def test_gev_live_release_keeps_one_row_per_metric_on_the_quarter_end(self):
        text = (FIX / "exhibit99_gev_2q26_live.txt").read_text(encoding="utf-8")
        self.assertIn("Orders of $24.2B", text)
        self.assertIn("on a U.S.", text)
        self.assertIn("led by Gas Power equipment", text)
        self.assertIn("data center orders", text)
        self.assertIn("reaching over $5 billion", text)
        gev = {
            "ticker": "GEV",
            "name": "GE Vernova Inc",
            "cik": "0001996810",
            "equipment": ["turbines", "transformers", "switchgear"],
        }
        accession = "0001996810-26-000147"
        rows = parser.parse_release(
            text,
            gev,
            "supplier_order",
            "8-K",
            "https://www.sec.gov/Archives/edgar/data/1996810/000199681026000147/gev-ex99.htm",
            "2026-07-22",
            accession=accession,
        )
        figures = dedupe_figures(pipeline.keep_distinct_amounts(live._figures(rows, "2026-07-22")))
        stored = {row["instrument"]: row for row in figures}
        expected = {
            "orders": 24_200_000_000,
            "revenue": 11_100_000_000,
            "backlog": 176_000_000_000,
            "orders:Power": 16_700_000_000,
            "revenue:Power": 5_500_000_000,
            "orders:Electrification": 6_300_000_000,
            "revenue:Electrification": 3_600_000_000,
            "backlog:Electrification": 40_600_000_000,
            "orders:Wind": 1_200_000_000,
            "revenue:Wind": 2_000_000_000,
            "orders:data center": 5_000_000_000,
        }
        self.assertEqual(set(stored), set(expected))
        for instrument, value in expected.items():
            row = stored[instrument]
            self.assertEqual(row["value"], value)
            self.assertEqual(row["period"], "2026-06-30")
            self.assertEqual(row["announced_on"], "2026-07-22")
            self.assertEqual(row["accession"], accession)
            self.assertNotIn("|", instrument)
        self.assertNotIn("2026-07-22", {row["period"] for row in figures})
        self.assertNotIn(16_729_000_000, {row["value"] for row in figures})
        self.assertNotIn(45_500_000_000, {row["value"] for row in figures})
        self.assertTrue(all("accession:" + accession in row["notes"] for row in rows if row.get("value") is not None))

        from shared.store import connect, save_run

        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "history.sqlite")
            conn = connect(db)
            try:
                run_id = save_run(conn, "orders-backlog", "2026-07-22T00:00:00Z", figures)
            finally:
                conn.close()
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                live._print_rows(db, run_id, figures)
            printed = stdout.getvalue()
        self.assertEqual(printed.count(f"accession={accession}"), len(figures))
        self.assertNotIn("accession=\n", printed)
        self.assertNotIn("orders|16700000000", printed)

    def test_etn_10q_stores_total_and_segment_backlog(self):
        text = (FIX / "etn_10q_backlog.txt").read_text(encoding="utf-8")
        etn = {
            "ticker": "ETN",
            "name": "Eaton Corp plc",
            "cik": "0001551182",
            "equipment": ["transformers", "switchgear"],
        }
        self.assertIn("governmental contractor", text)
        self.assertIn("December 31, 2025", text)
        rows = parser.parse_release(
            text, etn, "supplier_order", "10-Q", "https://www.sec.gov/etn-10q", "2026-08-05"
        )
        valued = [row for row in rows if row.get("value") is not None]
        disclosures = {row["disclosure"]: row for row in valued}
        self.assertEqual(disclosures["backlog"]["value"], 24_100_000_000)
        self.assertEqual(disclosures["backlog"]["equipment_or_period"], "2026-06-30")
        self.assertEqual(disclosures["backlog:Electrical Americas"]["value"], 15_175_000_000)
        self.assertEqual(disclosures["backlog:Electrical Global"]["value"], 3_602_000_000)
        self.assertEqual(disclosures["backlog:Aerospace"]["value"], 5_164_000_000)
        for row in valued:
            self.assertNotEqual(row["equipment_or_period"], "2025-12-31")
        self.assertEqual(pipeline.report_stale_latest(rows, "2026-10-12", stream=io.StringIO()), [])
        risk = (
            "There are certain factors that could cause actual results to differ materially, "
            "including rules associated with being a governmental contractor, as discussed in the "
            "Annual Report on Form 10-K for the fiscal year ended December 31, 2025."
        )
        risk_rows = parser.parse_release(
            risk, etn, "supplier_order", "10-Q", "https://www.sec.gov/etn-risk", "2026-08-05"
        )
        self.assertTrue(all(row.get("value") is None for row in risk_rows))
        self.assertEqual(pipeline.report_stale_latest(risk_rows, "2026-10-12", stream=io.StringIO()), [])

        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "history.sqlite")
            table = pipeline.build_table(releases=[{
                "company": etn,
                "body": text,
                "url": "https://www.sec.gov/etn-10q",
                "filing_date": "2026-08-05",
                "source_side": "supplier_order",
                "source_type": "10-Q",
            }])
            pipeline.store_run(db, table, "2026-08-05T00:00:00Z")
            conn = sqlite3.connect(db)
            stored = {
                row[0]: row[1]
                for row in conn.execute("SELECT instrument, value FROM figures")
            }
            conn.close()
        self.assertEqual(stored["backlog"], 24_100_000_000)
        self.assertEqual(stored["backlog:Electrical Americas"], 15_175_000_000)
        self.assertEqual(stored["backlog:Electrical Global"], 3_602_000_000)
        self.assertEqual(stored["backlog:Aerospace"], 5_164_000_000)

    def test_etn_html_10q_uses_the_balance_sheet_date_and_segment_cells(self):
        text = (FIX / "etn_10q_backlog.html").read_text(encoding="utf-8")
        self.assertIn("July 31, 2026", text)
        self.assertIn("June&nbsp;30,&nbsp;2026", text)
        self.assertIn("<td>Backlog</td><td>$15,175</td>", text)
        self.assertNotIn("| Backlog |", text)
        self.assertIn("governmental contractor", text)
        self.assertIn("December 31, 2025", text)
        etn = {
            "ticker": "ETN",
            "name": "Eaton Corp plc",
            "cik": "0001551182",
            "equipment": ["transformers", "switchgear"],
        }
        accession = "0001551182-26-000031"
        rows = parser.parse_release(
            text,
            etn,
            "supplier_order",
            "10-Q",
            "https://www.sec.gov/Archives/edgar/data/1551182/000155118226000031/eat-20260630.htm",
            "2026-07-31",
            accession=accession,
        )
        figures = dedupe_figures(pipeline.keep_distinct_amounts(live._figures(rows, "2026-07-31")))
        stored = {row["instrument"]: row for row in figures}
        self.assertEqual(set(stored), {
            "backlog",
            "backlog:Electrical Americas",
            "backlog:Electrical Global",
            "backlog:Aerospace",
        })
        self.assertEqual(stored["backlog"]["value"], 24_100_000_000)
        self.assertEqual(stored["backlog"]["period"], "2026-06-30")
        self.assertEqual(stored["backlog"]["announced_on"], "2026-07-31")
        self.assertEqual(stored["backlog:Electrical Americas"]["value"], 15_175_000_000)
        self.assertEqual(stored["backlog:Electrical Global"]["value"], 3_602_000_000)
        self.assertEqual(stored["backlog:Aerospace"]["value"], 5_164_000_000)
        for row in figures:
            self.assertEqual(row["period"], "2026-06-30")
            self.assertEqual(row["accession"], accession)
        self.assertEqual(pipeline.report_stale_latest(rows, "2026-10-12", stream=io.StringIO()), [])

    def test_etn_ixbrl_segment_cells_are_stored_and_logged(self):
        text = (FIX / "etn_10q_ixbrl.html").read_text(encoding="utf-8")
        self.assertIn("ix:nonFraction", text)
        self.assertIn("June&nbsp;30,&nbsp;2026", text)
        self.assertIn("<span>$</span>", text)
        self.assertIn(">15,175<", text)
        self.assertNotIn("$15,175", text)
        etn = {
            "ticker": "ETN",
            "name": "Eaton Corp plc",
            "cik": "0001551182",
            "equipment": ["transformers", "switchgear"],
        }
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            rows = parser.parse_release(
                text,
                etn,
                "supplier_order",
                "10-Q",
                "https://www.sec.gov/Archives/edgar/data/1551182/000155118226000030/etn-20260630.htm",
                "2026-07-31",
                accession="0001551182-26-000030",
            )
        logged = stdout.getvalue()
        figures = dedupe_figures(pipeline.keep_distinct_amounts(live._figures(rows, "2026-07-31")))
        stored = {row["instrument"]: row for row in figures}
        self.assertEqual(stored["backlog"]["value"], 24_100_000_000)
        self.assertEqual(stored["backlog"]["period"], "2026-06-30")
        self.assertEqual(stored["backlog"]["announced_on"], "2026-07-31")
        self.assertEqual(stored["backlog:Electrical Americas"]["value"], 15_175_000_000)
        self.assertEqual(stored["backlog:Electrical Global"]["value"], 3_602_000_000)
        self.assertEqual(stored["backlog:Aerospace"]["value"], 5_164_000_000)
        for row in figures:
            self.assertEqual(row["period"], "2026-06-30")
            self.assertNotEqual(row["period"], "2026-07-31")
        for segment, value in (
            ("Electrical Americas", 15_175_000_000),
            ("Electrical Global", 3_602_000_000),
            ("Aerospace", 5_164_000_000),
        ):
            self.assertIn(
                f"segment backlog ticker=ETN instrument=backlog:{segment} period=2026-06-30 value={value}",
                logged,
            )

    def test_rtx_backlog_is_the_quarter_end_total_not_free_cash_flow(self):
        text = (FIX / "exhibit99_rtx_2q26.txt").read_text(encoding="utf-8")
        self.assertIn("free cash flow* of $2.9 billion", text)
        self.assertIn("Company backlog of $289 billion", text)
        self.assertIn("our backlog is up 22 percent", text)
        self.assertIn("Quarter ended June 30, 2026", text)
        rtx = {"ticker": "RTX", "name": "RTX Corp", "cik": "0000101829", "sector": "defense"}
        rows = parser.parse_release(
            text,
            rtx,
            "customer_backlog",
            "8-K",
            "https://www.sec.gov/Archives/edgar/data/101829/000010182926000025/a2026-07x238xkerexhibit99.htm",
            "2026-07-23",
            accession="0000101829-26-000025",
        )
        valued = [row for row in rows if row.get("value") is not None]
        self.assertEqual(
            [(row["disclosure"], row["equipment_or_period"], row["value"]) for row in valued],
            [("backlog", "2026-06-30", 289_000_000_000)],
        )
        self.assertNotIn(2_900_000_000, {row["value"] for row in valued})
        self.assertNotIn("2026-07-23", {row["equipment_or_period"] for row in valued})
        self.assertEqual(valued[0]["filing_date"], "2026-07-23")

        ends = [
            "2024-06-30",
            "2024-09-30",
            "2024-12-31",
            "2025-03-31",
            "2025-06-30",
            "2025-09-30",
            "2025-12-31",
            "2026-03-31",
            "2026-06-30",
        ]
        facts = {
            "facts": {
                "us-gaap": {
                    "RevenueRemainingPerformanceObligation": {
                        "units": {
                            "USD": [
                                {
                                    "end": end,
                                    "val": 250_000_000_000 if end != "2026-06-30" else 289_000_000_000,
                                    "filed": "2026-07-23",
                                    "fp": "Q2",
                                    "fy": int(end[:4]),
                                    "form": "10-Q",
                                    "accn": "0000101829-26-000025",
                                }
                                for end in ends
                            ]
                        }
                    }
                }
            }
        }
        table = pipeline.build_table(
            customer_facts=[(rtx, facts)],
            releases=[
                {
                    "company": rtx,
                    "body": text,
                    "url": "https://www.sec.gov/Archives/edgar/data/101829/000010182926000025/a2026-07x238xkerexhibit99.htm",
                    "filing_date": "2026-07-23",
                    "source_side": "customer_backlog",
                    "source_type": "8-K",
                    "accession": "0000101829-26-000025",
                },
                {
                    "company": rtx,
                    "body": "For the quarter ended June 30, 2018, backlog was $200 billion.",
                    "url": "https://www.sec.gov/rtx-2018",
                    "filing_date": "2018-07-24",
                    "source_side": "customer_backlog",
                    "source_type": "8-K",
                },
            ],
        )
        valued_rows = [row for row in table["records"] if row.get("value") is not None and row["ticker"] == "RTX"]
        periods = {row["equipment_or_period"] for row in valued_rows}
        self.assertNotIn("2024-06-30", periods)
        self.assertNotIn("2018-06-30", periods)
        self.assertNotIn("2026-07-23", periods)
        self.assertEqual(
            periods,
            {
                "2024-09-30",
                "2024-12-31",
                "2025-03-31",
                "2025-06-30",
                "2025-09-30",
                "2025-12-31",
                "2026-03-31",
                "2026-06-30",
            },
        )
        self.assertIn(289_000_000_000, {row["value"] for row in valued_rows})
        self.assertNotIn(2_900_000_000, {row["value"] for row in valued_rows})

    def test_implausible_backlog_total_is_logged_and_skipped(self):
        figures = [
            {
                "ticker": "RTX",
                "figure_type": "customer_backlog",
                "instrument": "RevenueRemainingPerformanceObligation",
                "period": "2026-06-30|2026-07-23",
                "value": 289_000_000_000,
                "announced_on": "2026-07-23",
            },
            {
                "ticker": "RTX",
                "figure_type": "customer_backlog",
                "instrument": "backlog",
                "period": "2026-07-23",
                "value": 2_900_000_000,
                "announced_on": "2026-07-23",
            },
            {
                "ticker": "RTX",
                "figure_type": "customer_backlog",
                "instrument": "backlog:commercial",
                "period": "2026-06-30",
                "value": 170_000_000_000,
                "announced_on": "2026-07-23",
            },
        ]
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            kept = pipeline.drop_implausible_totals(figures)
        self.assertEqual(
            {(row["instrument"], row["value"]) for row in kept},
            {
                ("RevenueRemainingPerformanceObligation", 289_000_000_000),
                ("backlog:commercial", 170_000_000_000),
            },
        )
        self.assertIn(
            "skipped implausible backlog ticker=RTX value=2900000000 prior=289000000000 period=2026-07-23",
            stdout.getvalue(),
        )

    def test_etn_live_primary_document_stores_segments(self):
        text = (FIX / "etn_10q_live.html").read_text(encoding="utf-8")
        self.assertIn('name="us-gaap:RevenueRemainingPerformanceObligation" scale="9"', text)
        self.assertIn("June&#160;30, 2026", text)
        self.assertIn("Electrical Americas</span><table", text)
        self.assertIn('style="display:none"', text)
        self.assertIn("15,175&#160;", text)
        self.assertIn("($ in millions)", text)
        self.assertNotIn("$15,175", text)
        etn = {
            "ticker": "ETN",
            "name": "Eaton Corp plc",
            "cik": "0001551182",
            "equipment": ["transformers", "switchgear"],
        }
        index = {
            "directory": {
                "item": [
                    {"name": "0001551182-26-000030-index.html"},
                    {"name": "etn-20260630.htm"},
                    {"name": "etn06302026ex311.htm"},
                    {"name": "R1.htm"},
                    {"name": "R9.htm"},
                ]
            }
        }
        self.assertEqual(fetcher._primary_html(index, "etn-20260630.htm"), "etn-20260630.htm")
        self.assertEqual(fetcher._primary_html(index), "etn-20260630.htm")

        class Fake:
            def __init__(self):
                self.urls = []

            def json(self, url):
                self.urls.append(url)
                if url.endswith("index.json"):
                    return index
                return {
                    "filings": {
                        "recent": {
                            "form": ["10-K", "10-Q"],
                            "accessionNumber": [
                                "0001551182-26-000010",
                                "0001551182-26-000030",
                            ],
                            "filingDate": ["2026-02-24", "2026-07-31"],
                            "primaryDocument": ["etn-20251231.htm", "etn-20260630.htm"],
                        }
                    }
                }

            def get(self, url, accept=None):
                self.urls.append(url)
                self.got = url
                return (text.encode("utf-8"), "text/html")

        client = Fake()
        found = fetcher.collect_supplier_reports([etn], client=client)
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0]["url"].endswith("/etn-20260630.htm"))
        self.assertNotIn("R9.htm", found[0]["url"])
        self.assertEqual(found[0]["accession"], "0001551182-26-000030")
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            rows = parser.parse_release(
                found[0]["body"],
                etn,
                "supplier_order",
                "10-Q",
                found[0]["url"],
                found[0]["filing_date"],
                accession=found[0]["accession"],
            )
        logged = stdout.getvalue()
        figures = dedupe_figures(pipeline.keep_distinct_amounts(live._figures(rows, "2026-07-31")))
        stored = {row["instrument"]: row for row in figures}
        self.assertEqual(stored["backlog"]["value"], 24_100_000_000)
        self.assertEqual(stored["backlog"]["period"], "2026-06-30")
        self.assertEqual(stored["backlog:Electrical Americas"]["value"], 15_175_000_000)
        self.assertEqual(stored["backlog:Electrical Global"]["value"], 3_602_000_000)
        self.assertEqual(stored["backlog:Aerospace"]["value"], 5_164_000_000)
        for segment, value in (
            ("Electrical Americas", 15_175_000_000),
            ("Electrical Global", 3_602_000_000),
            ("Aerospace", 5_164_000_000),
        ):
            self.assertIn(
                f"segment backlog ticker=ETN instrument=backlog:{segment} period=2026-06-30 value={value}",
                logged,
            )
        noise = parser.parse_release(
            "Currency exchange contracts $6 Interest expense.",
            etn,
            "supplier_order",
            "10-Q",
            "https://www.sec.gov/etn-noise",
            "2026-07-31",
        )
        self.assertFalse([row for row in noise if row.get("value")])

    def test_hubb_intangible_backlog_is_not_stored(self):
        text = (FIX / "hubb_10q_intangible.html").read_text(encoding="utf-8")
        self.assertIn("FinitelivedIntangibleAssetsAcquired1", text)
        self.assertIn(">11.0<", text)
        self.assertIn("in millions, except useful life", text)
        self.assertIn("Net sales of $1,711.8 million", text)
        hubb = {
            "ticker": "HUBB",
            "name": "Hubbell Inc",
            "cik": "0000048898",
            "equipment": ["switchgear"],
        }
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            rows = parser.parse_release(
                text,
                hubb,
                "supplier_order",
                "10-Q",
                "https://www.sec.gov/Archives/edgar/data/48898/000162828026050405/hubb-20260630.htm",
                "2026-07-29",
                accession="0001628280-26-050405",
            )
        valued = [row for row in rows if row.get("value") is not None and row.get("disclosure") == "backlog"]
        self.assertEqual(valued, [])
        self.assertIn("skipped intangible backlog ticker=HUBB value=11000000", stdout.getvalue())
        floor = (
            "<p>For the quarterly period ended June 30, 2026</p>"
            "<p>Net sales of $1,711.8 million in the second quarter of 2026.</p>"
            "<table><tr><td>($ in millions)</td></tr>"
            "<tr><td>Backlog</td><td>$</td><td>11.0</td></tr></table>"
        )
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            rows = parser.parse_release(
                floor, hubb, "supplier_order", "10-Q", "https://www.sec.gov/hubb-floor", "2026-07-29"
            )
        self.assertFalse([row for row in rows if row.get("disclosure") == "backlog" and row.get("value")])
        self.assertIn(
            "skipped implausible backlog ticker=HUBB value=11000000 prior=1711800000 period=2026-06-30",
            stdout.getvalue(),
        )
        figures = [
            {
                "ticker": "HUBB",
                "figure_type": "supplier_order",
                "instrument": "backlog",
                "period": "2026-06-30",
                "value": 11_000_000,
            },
            {
                "ticker": "HUBB",
                "figure_type": "supplier_order",
                "instrument": "revenue",
                "period": "2026-06-30",
                "value": 1_711_800_000,
            },
        ]
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            kept = pipeline.drop_implausible_totals(figures)
        self.assertEqual([row["instrument"] for row in kept], ["revenue"])
        self.assertIn("prior=1711800000", stdout.getvalue())

    def test_same_quarter_press_release_does_not_duplicate_companyfacts(self):
        rtx_text = (FIX / "exhibit99_rtx_2q26.txt").read_text(encoding="utf-8")
        lmt_text = (FIX / "exhibit99_lmt_2q26.txt").read_text(encoding="utf-8")
        self.assertIn("Record backlog of $230 billion", lmt_text)
        self.assertIn("quarter ended June 28, 2026", lmt_text)
        rtx = {"ticker": "RTX", "name": "RTX Corp", "cik": "0000101829", "sector": "defense",
               "backlog_tags": ["RevenueRemainingPerformanceObligation"]}
        lmt = {"ticker": "LMT", "name": "Lockheed Martin Corp", "cik": "0000936468", "sector": "defense",
               "backlog_tags": ["RevenueRemainingPerformanceObligation"]}
        rtx_ends = [
            "2024-09-30",
            "2024-12-31",
            "2025-03-31",
            "2025-06-30",
            "2025-09-30",
            "2025-12-31",
            "2026-03-31",
            "2026-06-30",
        ]
        lmt_ends = [
            "2024-09-29",
            "2024-12-31",
            "2025-03-30",
            "2025-06-29",
            "2025-09-28",
            "2025-12-31",
            "2026-03-29",
            "2026-06-28",
        ]

        def facts(ends, latest, accn):
            return {
                "facts": {
                    "us-gaap": {
                        "RevenueRemainingPerformanceObligation": {
                            "units": {
                                "USD": [
                                    {
                                        "end": end,
                                        "val": latest if end == ends[-1] else 200_000_000_000,
                                        "filed": "2026-07-23",
                                        "fp": "Q2",
                                        "fy": int(end[:4]),
                                        "form": "10-Q",
                                        "accn": accn,
                                    }
                                    for end in ends
                                ]
                            }
                        }
                    }
                }
            }

        near = "Company backlog was $289 billion. For the quarter ended June 26, 2026."
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            table = pipeline.build_table(
                customer_facts=[
                    (rtx, facts(rtx_ends, 289_000_000_000, "0000101829-26-000099")),
                    (lmt, facts(lmt_ends, 230_400_000_000, "0001628280-26-049411")),
                ],
                releases=[
                    {
                        "company": rtx,
                        "body": rtx_text,
                        "url": "https://www.sec.gov/rtx",
                        "filing_date": "2026-07-23",
                        "source_side": "customer_backlog",
                        "source_type": "8-K",
                        "accession": "0000101829-26-000025",
                    },
                    {
                        "company": rtx,
                        "body": near,
                        "url": "https://www.sec.gov/rtx-near",
                        "filing_date": "2026-07-23",
                        "source_side": "customer_backlog",
                        "source_type": "8-K",
                    },
                    {
                        "company": lmt,
                        "body": lmt_text,
                        "url": "https://www.sec.gov/lmt",
                        "filing_date": "2026-07-23",
                        "source_side": "customer_backlog",
                        "source_type": "8-K",
                        "accession": "0001628280-26-049277",
                    },
                ],
            )
        logged = stdout.getvalue()
        self.assertIn("skipped duplicate backlog ticker=RTX", logged)
        self.assertIn("skipped duplicate backlog ticker=LMT", logged)
        for ticker, ends, kept_value, dropped_value in (
            ("RTX", rtx_ends, 289_000_000_000, None),
            ("LMT", lmt_ends, 230_400_000_000, 230_000_000_000),
        ):
            rows = [
                row for row in table["records"]
                if row.get("ticker") == ticker and row.get("value") is not None
            ]
            periods = {row["equipment_or_period"] for row in rows}
            self.assertLessEqual(len(periods), 8)
            self.assertEqual(periods, set(ends))
            latest = [row for row in rows if row["equipment_or_period"] == ends[-1]]
            self.assertEqual(len(latest), 1)
            self.assertEqual(latest[0]["value"], kept_value)
            self.assertEqual(latest[0]["source_type"], "xbrl_company_facts")
            if dropped_value is not None:
                self.assertNotIn(dropped_value, {row["value"] for row in rows})
        self.assertNotIn("2026-06-26", {row["equipment_or_period"] for row in table["records"] if row.get("value")})

    def test_gev_10q_narrative_does_not_store_comparatives_or_contract_lines(self):
        text = (FIX / "gev_10q_rpo.txt").read_text(encoding="utf-8")
        self.assertIn("RPO increased $26.0 billion", text)
        self.assertIn("RPO increased $47.6 billion", text)
        self.assertIn("increased $304 million", text)
        self.assertIn("higher contract losses", text)
        self.assertIn("from June 30, 2025", text)
        gev = {
            "ticker": "GEV",
            "name": "GE Vernova Inc",
            "cik": "0001996810",
            "equipment": ["turbines", "transformers", "switchgear"],
        }
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            rows = parser.parse_release(
                text,
                gev,
                "supplier_order",
                "10-Q",
                "https://www.sec.gov/Archives/edgar/data/1996810/000199681026000148/gev-20260630.htm",
                "2026-07-22",
                accession="0001996810-26-000148",
            )
        valued = [row for row in rows if row.get("value") is not None]
        values = {row["value"] for row in valued}
        for amount in (47_600_000_000, 26_000_000_000, 304_000_000, 300_000_000, 14_088_000_000, 700_000_000):
            self.assertNotIn(amount, values)
        periods = {row["equipment_or_period"] for row in valued}
        self.assertNotIn("2025-06-30", periods)
        self.assertNotIn("2026-07-22", periods)
        figures = live._figures(rows, "2026-07-22")
        self.assertFalse(figures)
        self.assertNotIn("transformers", {row["instrument"] for row in figures})

        comparative = (
            "For the quarter ended June 30, 2026, orders were $24.2 billion. "
            "Orders were $20.0 billion for the quarter ended June 30, 2025."
        )
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            rows = parser.parse_release(
                comparative, gev, "supplier_order", "10-Q", "https://www.sec.gov/gev-compare", "2026-07-22"
            )
        kept = [row for row in rows if row.get("value") is not None]
        self.assertEqual(
            [(row["disclosure"], row["equipment_or_period"], row["value"]) for row in kept],
            [("orders", "2026-06-30", 24_200_000_000)],
        )
        self.assertIn(
            "skipped prior-year comparative ticker=GEV value=20000000000 period=2025-06-30 document=2026-06-30",
            stdout.getvalue(),
        )
        named = parser.parse_release(
            (FIX / "exhibit99_order.txt").read_text(encoding="utf-8"),
            SUPPLIER, "supplier_order", "8-K", "https://www.sec.gov/order", "2026-06-02",
        )
        self.assertEqual(named[0]["disclosure"], "order")
        self.assertEqual(named[0]["counterparty"], "Harbour Utilities")
        self.assertEqual(named[0]["equipment_or_period"], "turbines")
        figure = live._figures(named, "2026-06-02")[0]
        self.assertEqual(figure["period"], "2026-06-02")
        self.assertEqual(figure["instrument"], "turbines")

    def test_supplier_10q_is_collected_without_a_socket(self):
        text = (FIX / "etn_10q_backlog.txt").read_text(encoding="utf-8")
        etn = {
            "ticker": "ETN",
            "name": "Eaton Corp plc",
            "cik": "0001551182",
            "equipment": ["transformers", "switchgear"],
        }
        tie = {
            "filings": {
                "recent": {
                    "form": ["10-K", "10-Q"],
                    "accessionNumber": ["0001551182-26-000010", "0001551182-26-000011"],
                    "filingDate": ["2026-02-24", "2026-02-24"],
                }
            }
        }
        self.assertEqual(fetcher.latest_periodic(tie)["form"], "10-Q")
        primary = fetcher._primary_html({
            "directory": {
                "item": [
                    {"name": "ex991.htm"},
                    {"name": "eat-20260630.htm"},
                ]
            }
        })
        self.assertEqual(primary, "eat-20260630.htm")

        class Fake:
            def __init__(self):
                self.urls = []

            def json(self, url):
                self.urls.append(url)
                if url.endswith("index.json"):
                    return {
                        "directory": {
                            "item": [
                                {"name": "ex991.htm"},
                                {"name": "eat-20260630.htm"},
                            ]
                        }
                    }
                return {
                    "filings": {
                        "recent": {
                            "form": ["10-K", "10-Q", "8-K"],
                            "accessionNumber": [
                                "0001551182-26-000010",
                                "0001551182-26-000020",
                                "0001551182-26-000030",
                            ],
                            "filingDate": ["2026-02-24", "2026-08-05", "2026-08-05"],
                        }
                    }
                }

            def get(self, url, accept=None):
                self.urls.append(url)
                return (text.encode("utf-8"), "text/html")

        client = Fake()
        found = fetcher.collect_supplier_reports([etn], client=client)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["form"], "10-Q")
        self.assertEqual(found[0]["filing_date"], "2026-08-05")
        self.assertEqual(found[0]["accession"], "0001551182-26-000020")
        self.assertTrue(found[0]["url"].endswith("/eat-20260630.htm"))
        self.assertTrue(all("sec.gov" in url for url in client.urls))
        self.assertNotIn("ex991.htm", found[0]["url"])
        rows = parser.parse_release(
            found[0]["body"], etn, "supplier_order", "10-Q", found[0]["url"], found[0]["filing_date"]
        )
        total = next(row for row in rows if row["disclosure"] == "backlog" and row["value"] == 24_100_000_000)
        self.assertEqual(total["equipment_or_period"], "2026-06-30")
        table = pipeline.build_table(releases=[{
            "company": etn,
            "body": found[0]["body"],
            "url": found[0]["url"],
            "filing_date": found[0]["filing_date"],
            "source_side": "supplier_order",
            "source_type": "10-Q",
            "accession": found[0]["accession"],
        }])
        valued = [row for row in table["records"] if row.get("value") is not None]
        self.assertTrue(valued)
        self.assertTrue(all("accession:0001551182-26-000020" in row["notes"] for row in valued))

    def test_one_table_shape(self):
        payload = json.loads((FIX / "amat_facts.json").read_text(encoding="utf-8"))
        order = (FIX / "exhibit99_order.txt").read_text(encoding="utf-8")
        table = pipeline.build_table(
            customer_facts=[(AMAT, payload)],
            releases=[{
                "company": SUPPLIER,
                "body": order,
                "url": "https://www.sec.gov/order",
                "filing_date": "2026-06-02",
                "source_side": "supplier_order",
                "source_type": "8-K",
            }],
        )
        self.assertEqual(table["scout"], "orders-backlog")
        sides = {row["source_side"] for row in table["records"]}
        self.assertEqual(sides, {"customer_backlog", "supplier_order"})
        keys = set(table["records"][0])
        for row in table["records"]:
            self.assertEqual(set(row), keys)
            pipeline.assert_shape(row)
        parsed = json.loads(pipeline.to_json(table))
        self.assertGreaterEqual(len(parsed["records"]), 2)

    def test_fetcher_company_facts_stays_on_the_client(self):
        payload = json.loads((FIX / "amat_facts.json").read_text(encoding="utf-8"))
        url = fetcher.company_facts_url("0000006951")
        self.assertTrue(url.startswith("https://data.sec.gov/api/xbrl/companyfacts/CIK0000006951.json"))

        class Fake:
            def __init__(self):
                self.calls = []

            def json(self, called):
                self.calls.append(called)
                return payload

        client = Fake()
        found = fetcher.collect_customer_facts([AMAT], client=client)
        self.assertEqual(client.calls, [url])
        self.assertEqual(found[0][1]["entityName"], "Applied Materials Inc")

    def test_default_sec_client_refuses_before_any_socket(self):
        from shared.nethttp import EndpointError

        os.environ.pop("SEC_CONTACT_EMAIL", None)
        with self.assertRaises(EndpointError):
            fetcher.collect_customer_facts([AMAT])

    def test_same_end_keeps_the_single_quarter_over_a_newer_ytd(self):
        payload = _orders(
            [
                {
                    "start": "2026-01-01",
                    "end": "2026-06-30",
                    "val": 900,
                    "filed": "2026-08-01",
                    "fp": "Q2",
                    "fy": 2026,
                    "form": "10-Q",
                    "accn": "0000999001-26-000008",
                },
                {
                    "start": "2026-04-01",
                    "end": "2026-06-30",
                    "val": 400,
                    "filed": "2026-05-01",
                    "fp": "Q2",
                    "fy": 2026,
                    "form": "10-Q",
                    "frame": "CY2026Q2",
                    "accn": "0000999001-26-000004",
                },
            ]
        )
        with self.assertLogs("shared.store", level="WARNING") as caught:
            rows = parser.parse_company_facts(payload, SUPPLIER, "supplier_order")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["value"], 400)
        self.assertEqual(rows[0]["filing_date"], "2026-05-01")
        self.assertIn("conflicting_value", rows[0]["flags"])
        self.assertTrue(any("value=900" in line and "kept value=400" in line for line in caught.output))

    def test_same_span_keeps_the_newer_filing(self):
        payload = _orders(
            [
                {
                    "start": "2026-04-01",
                    "end": "2026-06-30",
                    "val": 400,
                    "filed": "2026-05-01",
                    "fp": "Q2",
                    "form": "10-Q",
                    "accn": "0000999001-26-000004",
                },
                {
                    "start": "2026-04-01",
                    "end": "2026-06-30",
                    "val": 450,
                    "filed": "2026-08-01",
                    "fp": "Q2",
                    "form": "10-Q",
                    "accn": "0000999001-26-000009",
                },
            ]
        )
        with self.assertLogs("shared.store", level="WARNING") as caught:
            rows = parser.parse_company_facts(payload, SUPPLIER, "supplier_order")
        self.assertEqual(rows[0]["value"], 450)
        self.assertEqual(rows[0]["filing_date"], "2026-08-01")
        self.assertTrue(any("value=400" in line and "kept value=450" in line for line in caught.output))

    def test_live_duplicate_currencies_store_one_figure(self):
        payload = {
            "cik": 6951,
            "entityName": "Applied Materials Inc",
            "facts": {
                "us-gaap": {
                    "RevenueRemainingPerformanceObligation": {
                        "units": {
                            "USD": [
                                {
                                    "end": "2026-04-26",
                                    "val": 100,
                                    "accn": "0000006951-26-000001",
                                    "fy": 2026,
                                    "fp": "Q2",
                                    "form": "10-Q",
                                    "filed": "2026-06-01",
                                },
                                {
                                    "end": "2025-10-26",
                                    "val": 50,
                                    "accn": "0000006951-25-000040",
                                    "fy": 2025,
                                    "fp": "FY",
                                    "form": "10-K",
                                    "filed": "2025-12-01",
                                },
                            ],
                            "EUR": [
                                {
                                    "end": "2026-04-26",
                                    "val": 200,
                                    "accn": "0000006951-26-000009",
                                    "fy": 2026,
                                    "fp": "Q2",
                                    "form": "10-Q",
                                    "filed": "2026-06-01",
                                }
                            ],
                        }
                    }
                }
            },
        }
        previous = os.environ.pop("SEC_CONTACT_EMAIL", None)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                fixture = Path(tmp) / "facts.json"
                fixture.write_text(json.dumps(payload), encoding="utf-8")
                db = str(Path(tmp) / "history.sqlite")
                with self.assertLogs("shared.store", level="WARNING") as caught:
                    code = live.main(["--db", db, "--sec-fixture", str(fixture), "--no-asx"])
                self.assertEqual(code, 0)
                conn = sqlite3.connect(db)
                conn.row_factory = sqlite3.Row
                run = conn.execute("SELECT scout FROM runs").fetchone()["scout"]
                rows = conn.execute(
                    "SELECT period, value, currency FROM figures ORDER BY period"
                ).fetchall()
                conn.close()
        finally:
            if previous is not None:
                os.environ["SEC_CONTACT_EMAIL"] = previous
        self.assertEqual(run, "orders-backlog")
        stored = {(row["period"], row["currency"]): row["value"] for row in rows}
        self.assertEqual(
            stored,
            {("2025-10-26|2025-12-01", "USD"): 50, ("2026-04-26|2026-06-01", "USD"): 100},
        )
        self.assertTrue(any("value=200" in line and "kept value=100" in line for line in caught.output))

    def test_ir_urls_are_the_only_non_sec_asx_fetches(self):
        from shared.nethttp import EndpointError

        company = dict(SUPPLIER, ir_urls=["https://www.sec.gov/news"])
        with self.assertRaises(EndpointError):
            fetcher.collect_ir([company], client=object())

    def test_same_day_runs_keep_their_figures(self):
        from model import record

        def table(value):
            return {
                "records": [
                    record(
                        source_side="supplier_order",
                        company="Northwind Turbines",
                        ticker="NWT",
                        source_url="https://www.sec.gov/order",
                        filing_date="2026-06-02",
                        source_type="8-K",
                        disclosure="order",
                        counterparty="Harbour Utilities",
                        equipment_or_period="turbines",
                        value=value,
                        currency="USD",
                    )
                ]
            }

        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "history.sqlite")
            first = pipeline.store_run(db, table(180_000_000), "2026-10-12T08:15:00Z")
            second = pipeline.store_run(db, table(40_000_000), "2026-10-12T18:45:09Z")
            self.assertEqual(first["run_id"], "2026-10-12-081500-orders-backlog")
            self.assertEqual(second["run_id"], "2026-10-12-184509-orders-backlog")
            conn = sqlite3.connect(db)
            morning = conn.execute("SELECT value FROM figures WHERE run_id = ?", (first["run_id"],)).fetchone()[0]
            afternoon = conn.execute("SELECT value FROM figures WHERE run_id = ?", (second["run_id"],)).fetchone()[0]
            self.assertEqual(morning, 180_000_000)
            self.assertEqual(afternoon, 40_000_000)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 2)
            conn.close()

    def test_asx_markit_keeps_order_headlines(self):
        from shared.nethttp import EndpointError

        url = "https://asx.api.markitdigital.com/asx-research/1.0/companies/wor/announcements?count=5"

        class Fake:
            def __init__(self, payload):
                self.payload = payload
                self.urls = []

            def json(self, called):
                self.urls.append(called)
                return self.payload

            def get(self, called, accept=""):
                self.urls.append(called)
                return b"Order for turbines worth $10 million", "text/plain"

        with self.assertRaises(EndpointError):
            fetcher.collect_asx([{"ticker": "WOR"}], client=Fake({"data": []}))
        payload = {
            "data": {
                "displayName": "Worley Ltd",
                "items": [
                    {"headline": "Worley awarded turbine order", "documentKey": "key-1", "date": "2026-10-01"},
                    {"headline": "Dividend", "documentKey": "key-2", "date": "2026-10-02"},
                ],
            }
        }
        documents = fetcher.collect_asx([{"ticker": "WOR", "name": "Worley"}], client=Fake(payload))
        self.assertEqual(len(documents), 1)
        self.assertIn("key-1", documents[0]["url"])
        self.assertIn("turbines", documents[0]["body"])
        self.assertEqual(documents[0]["form"], "ASX")
        self.assertEqual(documents[0]["filing_date"], "2026-10-01")

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
                        str(FIX / "amat_facts.json"),
                        "--no-asx",
                    ]
                )
                self.assertEqual(code, 0)
                conn = sqlite3.connect(db)
                rows = conn.execute(
                    "SELECT ticker, figure_type, instrument, value, verified FROM figures ORDER BY value"
                ).fetchall()
                conn.close()
                values = {row[3]: row for row in rows}
                self.assertIn(12400000000, values)
                clean = values[12400000000]
                self.assertEqual(clean[0], "AMAT")
                self.assertEqual(clean[1], "customer_backlog")
                self.assertEqual(clean[2], "RevenueRemainingPerformanceObligation")
                self.assertEqual(clean[4], 1)
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
                [sys.executable, "-m", "Scouts.orders_backlog.live", flag],
                cwd=root,
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("usage", completed.stdout.lower())


def _orders(points):
    return {"facts": {"us-gaap": {"OrdersReceived": {"units": {"USD": points}}}}}


if __name__ == "__main__":
    unittest.main()
