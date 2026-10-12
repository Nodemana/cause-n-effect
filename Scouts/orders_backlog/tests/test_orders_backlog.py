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

import fetcher  # noqa: E402
import live  # noqa: E402
import parser  # noqa: E402
import pipeline  # noqa: E402
from tags import CUSTOMER_SECTORS, PROXY_TAGS, SUPPLIER_EQUIPMENT  # noqa: E402

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
        self.assertEqual(included[0]["counterparty"], None)
        self.assertTrue(all(row["value"] != 3100000000 for row in rows))
        self.assertEqual(review[0]["flags"], ["stale_tag", "tag_switch"])
        self.assertEqual(review[0]["value"], 9000000000)
        periods = {row["equipment_or_period"] for row in rows}
        self.assertNotIn("2018-04-29", periods)
        self.assertNotIn(2100000000, {row["value"] for row in rows})

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
