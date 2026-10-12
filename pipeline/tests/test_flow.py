"""Full sweep against fixture figures. The live commands never call a model."""

import io
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from pipeline.agent import AgentResultError  # noqa: E402
from pipeline.cli import main  # noqa: E402
import pipeline.flow as flow  # noqa: E402
from pipeline.fixture import fixture_response, run_with_fixture  # noqa: E402
from pipeline.flow import ingest_run, pipeline_status, run_pipeline  # noqa: E402
from shared.pipeline_store import connect, lodge_lead  # noqa: E402
from shared.store import save_run  # noqa: E402

AMZN_URL = "https://www.sec.gov/Archives/edgar/data/1018724/000101872426000001/amzn.htm"
GEV_URL = "https://www.sec.gov/Archives/edgar/data/2088000/000208800026000001/gev.htm"
MSFT_URL = "https://www.sec.gov/Archives/edgar/data/789019/000078901926000001/msft.xml"
ORCL_URL = "https://www.sec.gov/Archives/edgar/data/1341439/000134143926000001/orcl.htm"
ORCL_PRIOR = "https://www.sec.gov/Archives/edgar/data/1341439/000134143925000001/prior.htm"


class PipelineFlowTests(unittest.TestCase):
    def setUp(self):
        self._utc_now = flow.utc_now
        flow.utc_now = lambda: "2026-10-12T18:00:00Z"

    def tearDown(self):
        flow.utc_now = self._utc_now

    def test_full_sweep_with_stub(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "history.sqlite")
            _seed(path)
            result = run_with_fixture(path)
            self.assertEqual(result["provider"], "agent")
            self.assertEqual(result["tokens_used"], 0)
            self.assertEqual(result["token_budget"], 0)
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["stage"], "legate_selection")
            self.assertEqual(result["macro"], "skipped")
            self.assertFalse(result["dry_run"])
            self.assertEqual(result["merges"], 0)
            self.assertEqual(result["decisions"], 3)
            self.assertEqual(result["rankings"], 3)
            report_path = Path(result["report"])
            candidates_path = Path(result["candidates"])
            self.assertEqual(report_path.parent.name, "reports")
            self.assertEqual(report_path.name, f"{result['run_id']}-final.md")
            self.assertEqual(candidates_path.name, f"{result['run_id']}-candidates.md")
            report = report_path.read_text(encoding="utf-8")
            candidates = candidates_path.read_text(encoding="utf-8")
            self.assertEqual(report.count("\n## Rank "), 3)
            self.assertEqual(candidates.count("\n## Rank "), 3)
            self.assertIn("Spender AMZN", report)
            self.assertIn("Supplier (backlog) GEV", report)
            self.assertIn(AMZN_URL, report)
            self.assertIn(GEV_URL, report)
            self.assertIn("### Thesis", report)
            self.assertIn("### Cause → effect", report)
            self.assertIn("**Tickers:**", report)
            self.assertIn("### Evidence", report)
            self.assertIn("### Praetorian", report)
            self.assertIn("### Praetorian", candidates)
            self.assertIn("No flaws recorded.", report)
            self.assertIn("**Lead id:**", report)
            self.assertIn("**Legate rationale:**", report)
            self.assertNotIn("Stage skipped.", report)
            self.assertNotIn("**Verdict:**", report)
            self.assertTrue(any(prompt.startswith("TASK: praetorian") for prompt in result["prompts"]))
            self.assertTrue(any(prompt.startswith("TASK: legate_selection") for prompt in result["prompts"]))
            self.assertIn("### Macro context", report)
            self.assertIn("Sweep macro context: none", report)

            conn = connect(path)
            try:
                self._assert_migration_once(conn)
                self._assert_rejects(conn)
                self._assert_leads_and_theses(conn, result["prompts"])
                self._assert_merge_rank_and_decisions(conn)
                before = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
            finally:
                conn.close()

            again = run_with_fixture(path)
            conn = connect(path)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0], before)
                self.assertGreater(again["rejects"], 0)
            finally:
                conn.close()

    def test_priced_in_and_macro_cache_on_the_capex_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "history.sqlite")
            cache = Path(tmp) / "macro.json"
            cache.write_text(json.dumps({"summary": "Policy rates unchanged."}), encoding="utf-8")
            conn = _history(path)
            try:
                save_run(
                    conn,
                    "capex",
                    "2026-10-12T15:00:00Z",
                    [
                        _figure(
                            "AMZN",
                            "Amazon",
                            "PaymentsToAcquireProductiveAssets",
                            "quarter",
                            "2026-07-01/2026-09-30",
                            5_000_000_000,
                            "priced-in supplier: Globex Power",
                            AMZN_URL,
                        ),
                        _figure(
                            "AMZN",
                            "Amazon",
                            "PaymentsToAcquirePropertyPlantAndEquipment",
                            "quarter",
                            "2025-07-01/2025-09-30",
                            1_000_000_000,
                            "1000000000",
                            AMZN_URL + "#prior",
                        ),
                        *_caps("AMZN", "GEV"),
                    ],
                )
                save_run(
                    conn,
                    "orders-backlog",
                    "2026-10-12T15:01:00Z",
                    [
                        _figure(
                            "GEV",
                            "Globex Power",
                            "supplier_order",
                            "Amazon",
                            "2026-10-01",
                            2_000_000_000,
                            "2000000000 buyer: Amazon",
                            GEV_URL,
                        ),
                        _figure(
                            "GEV",
                            "Globex Power",
                            "customer_backlog",
                            "RevenueRemainingPerformanceObligation",
                            "2026-06-30|2026-07-22",
                            40_000_000,
                            "40000000",
                            GEV_URL + "#prior",
                        ),
                    ],
                )
            finally:
                conn.close()
            result = run_with_fixture(path, macro_cache=str(cache))
            conn = connect(path)
            try:
                merged = conn.execute(
                    """
                    SELECT t.price_run, t.macro_context, t.assumptions, t.pass_number, l.ticker, l.source_scout
                    FROM theses AS t
                    JOIN leads AS l ON l.lead_id = t.lead_id
                    WHERE l.source_scout = 'capex_chain'
                    """
                ).fetchone()
                self.assertEqual(merged["price_run"], "priced-in")
                self.assertEqual(merged["pass_number"], 1)
                self.assertEqual(merged["source_scout"], "capex_chain")
                self.assertEqual(merged["macro_context"], "Policy rates unchanged.")
                self.assertIn("Macro context used: Policy rates unchanged.", merged["assumptions"])
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM theses WHERE pass_number = 2").fetchone()[0],
                    0,
                )
                review = conn.execute(
                    """
                    SELECT r.flaws, k.rank, k.surviving_issues
                    FROM praetorian_reviews AS r
                    JOIN theses AS t ON t.thesis_id = r.thesis_id
                    JOIN leads AS l ON l.lead_id = t.lead_id
                    JOIN rankings AS k ON k.lead_id = l.lead_id
                    WHERE l.source_scout = 'capex_chain'
                    """
                ).fetchone()
                flaws = json.loads(review["flaws"])
                self.assertTrue(flaws)
                priced = next(item for item in flaws if "priced-in" in item["flaw"])
                self.assertEqual(priced["severity"], "high")
                self.assertIn("priced-in", priced["evidence"])
                self.assertEqual(review["rank"], 1)
                self.assertEqual(review["surviving_issues"], 0)
                report = Path(result["report"]).read_text(encoding="utf-8")
                self.assertIn("Policy rates unchanged.", report)
                self.assertIn("### Macro context", report)
                self.assertIn("priced-in", report)
                self.assertIn("**high**", report)
                self.assertIn("Evidence:", report)
            finally:
                conn.close()

    def test_macro_tables_outrank_the_json_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "history.sqlite")
            cache = Path(tmp) / "macro.json"
            cache.write_text(json.dumps({"summary": "JSON cache should not be used."}), encoding="utf-8")
            conn = _history(path)
            try:
                save_run(
                    conn,
                    "capex",
                    "2026-10-12T15:00:00Z",
                    [
                        _figure(
                            "AMZN",
                            "Amazon",
                            "PaymentsToAcquireProductiveAssets",
                            "quarter",
                            "2026-07-01/2026-09-30",
                            5_000_000_000,
                            "5000000000 supplier: Globex Power",
                            AMZN_URL,
                        ),
                        _figure(
                            "AMZN",
                            "Amazon",
                            "PaymentsToAcquirePropertyPlantAndEquipment",
                            "quarter",
                            "2025-07-01/2025-09-30",
                            1_000_000_000,
                            "1000000000",
                            AMZN_URL + "#prior",
                        ),
                        *_caps("AMZN", "GEV"),
                    ],
                )
                save_run(
                    conn,
                    "orders-backlog",
                    "2026-10-12T15:01:00Z",
                    [
                        _figure(
                            "GEV",
                            "Globex Power",
                            "supplier_order",
                            "Amazon",
                            "2026-10-01",
                            2_000_000_000,
                            "2000000000 buyer: Amazon",
                            GEV_URL,
                        ),
                        _figure(
                            "GEV",
                            "Globex Power",
                            "customer_backlog",
                            "RevenueRemainingPerformanceObligation",
                            "2026-06-30|2026-07-22",
                            40_000_000,
                            "40000000",
                            GEV_URL + "#prior",
                        ),
                    ],
                )
            finally:
                conn.close()
            conn = connect(path)
            try:
                conn.execute(
                    """
                    CREATE TABLE macro_series (
                        series_id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        source TEXT NOT NULL,
                        source_url TEXT NOT NULL,
                        unit TEXT NOT NULL,
                        frequency TEXT NOT NULL,
                        stale_after_hours INTEGER NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE macro_observations (
                        series_id TEXT NOT NULL REFERENCES macro_series(series_id),
                        obs_date TEXT NOT NULL,
                        value REAL NOT NULL,
                        fetched_at TEXT NOT NULL,
                        UNIQUE (series_id, obs_date)
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO macro_series (
                        series_id, name, source, source_url, unit, frequency, stale_after_hours
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "DGS10",
                        "10-Year Treasury",
                        "FRED",
                        "https://fred.stlouisfed.org/series/DGS10",
                        "percent",
                        "daily",
                        24,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO macro_observations (series_id, obs_date, value, fetched_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    ("DGS10", "2026-10-01", 4.2, "2026-10-12T00:00:00Z"),
                )
                conn.execute(
                    """
                    INSERT INTO macro_observations (series_id, obs_date, value, fetched_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    ("DGS10", "2026-09-01", 3.9, "2026-09-02T00:00:00Z"),
                )
                conn.commit()
            finally:
                conn.close()
            run_with_fixture(path, macro_cache=str(cache))
            conn = connect(path)
            try:
                merged = conn.execute(
                    """
                    SELECT t.macro_context, t.assumptions
                    FROM theses AS t
                    JOIN leads AS l ON l.lead_id = t.lead_id
                    WHERE l.source_scout = 'capex_chain'
                    """
                ).fetchone()
                self.assertIn("DGS10", merged["macro_context"])
                self.assertIn("4.2", merged["macro_context"])
                self.assertIn("2026-10-01", merged["macro_context"])
                self.assertNotIn("2026-09-01", merged["macro_context"])
                self.assertNotIn("JSON cache should not be used.", merged["macro_context"])
                self.assertIn("Macro context used:", merged["assumptions"])
                self.assertNotIn("JSON cache should not be used.", merged["assumptions"])
                self.assertGreater(
                    conn.execute("SELECT COUNT(*) FROM praetorian_reviews").fetchone()[0],
                    0,
                )
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM praetorian_reviews").fetchone()[0],
                    conn.execute("SELECT COUNT(*) FROM rankings").fetchone()[0],
                )
            finally:
                conn.close()

    def test_second_pass_stores_macro_context_from_the_scout_tables(self):
        from Scouts.macro import catalog
        from Scouts.macro import store as macro_store

        saved = {name: os.environ.get(name) for name in ("FRED_API_KEY", "EIA_API_KEY")}
        os.environ.pop("FRED_API_KEY", None)
        os.environ.pop("EIA_API_KEY", None)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = str(Path(tmp) / "history.sqlite")
                conn = macro_store.open_db(path)
                try:
                    save_run(
                        conn,
                        "capex",
                        "2026-10-12T15:00:00Z",
                        [
                            _figure(
                                "AMZN",
                                "Amazon",
                                "PaymentsToAcquireProductiveAssets",
                                "quarter",
                                "2026-07-01/2026-09-30",
                                5_000_000_000,
                                "5000000000 supplier: Globex Power",
                                AMZN_URL,
                            ),
                            _figure(
                                "AMZN",
                                "Amazon",
                                "PaymentsToAcquirePropertyPlantAndEquipment",
                                "quarter",
                                "2025-07-01/2025-09-30",
                                1_000_000_000,
                                "1000000000",
                                AMZN_URL + "#prior",
                            ),
                            *_caps("AMZN", "GEV"),
                        ],
                    )
                    save_run(
                        conn,
                        "orders-backlog",
                        "2026-10-12T15:01:00Z",
                        [
                            _figure(
                                "GEV",
                                "Globex Power",
                                "supplier_order",
                                "Amazon",
                                "2026-10-01",
                                2_000_000_000,
                                "2000000000 buyer: Amazon",
                                GEV_URL,
                            ),
                            _figure(
                                "GEV",
                                "Globex Power",
                                "customer_backlog",
                                "RevenueRemainingPerformanceObligation",
                                "2026-06-30|2026-07-22",
                                40_000_000,
                                "40000000",
                                GEV_URL + "#prior",
                            ),
                        ],
                    )
                    for series_id, observations in (
                        ("DGS10", [{"obs_date": "2026-09-01", "value": 3.9}, {"obs_date": "2026-10-01", "value": 4.2}]),
                        ("RBA_CASH", [{"obs_date": "2026-10-08", "value": 3.6}]),
                    ):
                        macro_store.upsert_series(conn, catalog.by_id()[series_id])
                        macro_store.save_observations(conn, series_id, observations, "2026-10-12T00:00:00Z")
                    conn.commit()
                finally:
                    conn.close()
                result = run_with_fixture(path)
                conn = connect(path)
                try:
                    merged = conn.execute(
                        """
                        SELECT t.macro_context
                        FROM theses AS t
                        JOIN leads AS l ON l.lead_id = t.lead_id
                        WHERE l.source_scout = 'capex_chain'
                        """
                    ).fetchone()
                    first = conn.execute(
                        """
                        SELECT t.macro_context
                        FROM theses AS t
                        JOIN leads AS l ON l.lead_id = t.lead_id
                        WHERE l.source_scout != 'capex_chain'
                        """
                    ).fetchall()
                finally:
                    conn.close()
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        self.assertEqual(result["macro"], "loaded (second-pass theses: 1)")
        context = merged["macro_context"]
        head, _, tail = context.partition("unavailable:")
        self.assertIn("DGS10", head)
        self.assertIn("4.2", head)
        self.assertIn("2026-10-01", head)
        self.assertNotIn("2026-09-01", head)
        self.assertIn("RBA_CASH", head)
        self.assertIn("3.6", head)
        self.assertNotIn("DGS10", tail)
        self.assertIn("FRED (no key): DFF, DGS2", tail)
        self.assertIn("EIA (no key): WTI, BRENT, HENRY_HUB", tail)
        self.assertIn("BLS (not loaded): US_CPI", tail)
        for row in first:
            self.assertIsNone(row["macro_context"])

    def test_capex_buyer_and_supplier_backlog_merge_end_to_end(self):
        summary = "Treasury yields are the macro context for this pass."
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "history.sqlite")
            cache = Path(tmp) / "macro.json"
            cache.write_text(json.dumps({"summary": summary}), encoding="utf-8")
            conn = _history(path)
            try:
                save_run(
                    conn,
                    "capex",
                    "2026-10-12T15:00:00Z",
                    [
                        _figure(
                            "AMZN",
                            "Amazon",
                            "PaymentsToAcquireProductiveAssets",
                            "quarter",
                            "2026-07-01/2026-09-30",
                            5_000_000_000,
                            "5000000000 supplier: Globex Power",
                            AMZN_URL,
                        ),
                        _figure(
                            "AMZN",
                            "Amazon",
                            "PaymentsToAcquirePropertyPlantAndEquipment",
                            "quarter",
                            "2025-07-01/2025-09-30",
                            1_000_000_000,
                            "1000000000",
                            AMZN_URL + "#prior",
                        ),
                        *_caps("AMZN", "GEV"),
                    ],
                )
                save_run(
                    conn,
                    "orders-backlog",
                    "2026-10-12T15:01:00Z",
                    [
                        _figure(
                            "GEV",
                            "Globex Power",
                            "supplier_order",
                            "Amazon",
                            "2026-10-01",
                            2_000_000_000,
                            "2000000000 buyer: Amazon",
                            GEV_URL,
                        ),
                        _figure(
                            "GEV",
                            "Globex Power",
                            "customer_backlog",
                            "RevenueRemainingPerformanceObligation",
                            "2026-06-30|2026-07-22",
                            40_000_000,
                            "40000000",
                            GEV_URL + "#prior",
                        ),
                    ],
                )
            finally:
                conn.close()
            result = run_with_fixture(path, macro_cache=str(cache))
            report_text = Path(result["report"]).read_text(encoding="utf-8")
            conn = connect(path)
            try:
                self.assertEqual(result["merges"], 0)
                merged = conn.execute(
                    """
                    SELECT t.thesis_text, t.macro_context, t.pass_number, t.roi, l.source_scout, l.lead_id
                    FROM theses AS t
                    JOIN leads AS l ON l.lead_id = t.lead_id
                    WHERE l.source_scout = 'capex_chain'
                    """
                ).fetchone()
                reviews = conn.execute("SELECT COUNT(*) FROM praetorian_reviews").fetchone()[0]
                ranking = conn.execute(
                    """
                    SELECT k.rank, l.source_scout
                    FROM rankings AS k
                    JOIN leads AS l ON l.lead_id = k.lead_id
                    """
                ).fetchone()
                evidence = conn.execute(
                    """
                    SELECT f.source_url, f.ticker
                    FROM lead_figures AS link
                    JOIN figures AS f ON f.figure_id = link.figure_id
                    WHERE link.lead_id = ?
                    """,
                    (merged["lead_id"],),
                ).fetchall()
            finally:
                conn.close()
        self.assertEqual(merged["source_scout"], "capex_chain")
        self.assertEqual(merged["pass_number"], 1)
        self.assertEqual(merged["macro_context"], summary)
        self.assertIn("Globex Power", merged["thesis_text"])
        self.assertIn("Amazon", merged["thesis_text"])
        self.assertEqual(merged["roi"], "27%")
        self.assertGreater(reviews, 0)
        self.assertEqual(reviews, result["rankings"])
        self.assertEqual(ranking["rank"], 1)
        self.assertEqual(ranking["source_scout"], "capex_chain")
        self.assertEqual({item["source_url"] for item in evidence}, {AMZN_URL, GEV_URL})
        self.assertEqual({item["ticker"] for item in evidence}, {"AMZN", "GEV"})
        self.assertIn(summary, report_text)
        self.assertIn("### Macro context", report_text)

    def test_run_stops_before_judgement(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "history.sqlite")
            queue = str(Path(tmp) / "agent_queue")
            conn = _history(path)
            try:
                save_run(
                    conn,
                    "insider-buying",
                    "2026-10-12T16:00:00Z",
                    [
                        _figure("MSFT", "Microsoft", "insider_purchase", "ceo", "2026-10-01|Ada", 250000, "250000 shares", MSFT_URL),
                        *_caps("MSFT"),
                    ],
                )
            finally:
                conn.close()
            result = run_pipeline(path, queue_dir=queue)
            self.assertEqual(result["status"], "awaiting_agents")
            self.assertEqual(result["stage"], "significance")
            self.assertEqual(result["tokens_used"], 0)
            self.assertEqual(result["leads"], 0)
            self.assertGreater(result["tasks"], 0)
            report = pipeline_status(path, result["run_id"])
            self.assertEqual(len(report["outstanding"]), result["tasks"])
            task = json.loads(Path(report["outstanding"][0]["task"]).read_text(encoding="utf-8"))
            self.assertTrue(task["prompt"].startswith("TASK: significance"))
            self.assertIn("output_schema", task)
            self.assertIn("rules", task)
            conn = connect(path)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM leads WHERE run_id = ?", (result["run_id"],)).fetchone()[0], 0)
                reasons = [row["reason"] for row in conn.execute("SELECT reason FROM lead_rejects")]
                self.assertFalse(any("token budget" in reason for reason in reasons))
            finally:
                conn.close()

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "history.sqlite")
            queue = Path(tmp) / "agent_queue"
            _seed(path)
            result = run_pipeline(path, dry_run=True, queue_dir=str(queue))
            self.assertTrue(result["dry_run"])
            self.assertIsNone(result["run_id"])
            self.assertGreater(result["tasks"], 0)
            self.assertEqual(result["leads"], 0)
            self.assertFalse(queue.exists())
            self.assertIsNone(result["report"])
            self.assertFalse((Path(tmp) / "reports").exists())
            conn = connect(path)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM lead_rejects").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM pipeline_runs").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM theses").fetchone()[0], 0)
            finally:
                conn.close()

    def test_help_does_not_open_a_socket(self):
        completed = subprocess.run(
            [sys.executable, "-m", "pipeline", "run", "--help"],
            cwd=REPO,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertNotIn("token-budget", completed.stdout)
        self.assertNotIn("LLM_API_KEY", completed.stdout)
        self.assertNotIn("LLM_BASE_URL", completed.stdout)
        self.assertNotIn("LLM_MODEL", completed.stdout)
        self.assertIn("awaiting_agents", completed.stdout)
        self.assertIn("agent_queue", completed.stdout)
        self.assertNotIn("--max-theses", completed.stdout)
        self.assertIn("legate_selection", completed.stdout)
        self.assertIn("-candidates.md", completed.stdout)
        self.assertIn("-final.md", completed.stdout)
        self.assertNotIn("--with-praetorians", completed.stdout)
        self.assertIn("data/reports", completed.stdout)
        self.assertNotIn("TODO", completed.stdout)
        self.assertNotIn("yahoo", completed.stdout.casefold())

        def bomb(*_args, **_kwargs):
            raise AssertionError("socket opened")

        real_socket = socket.socket
        real_open = urllib.request.urlopen
        socket.socket = bomb
        urllib.request.urlopen = bomb
        try:
            with self.assertRaises(SystemExit) as caught:
                main(["run", "--help"])
            self.assertEqual(caught.exception.code, 0)
            with self.assertRaises(SystemExit) as caught:
                main(["rejects", "--help"])
            self.assertEqual(caught.exception.code, 0)
            with self.assertRaises(SystemExit) as caught:
                main(["status", "--help"])
            self.assertEqual(caught.exception.code, 0)
            with self.assertRaises(SystemExit) as caught:
                main(["ingest", "--help"])
            self.assertEqual(caught.exception.code, 0)
        finally:
            socket.socket = real_socket
            urllib.request.urlopen = real_open

    def test_legate_selection_chooses_the_final_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "history.sqlite")
            queue = str(Path(tmp) / "agent_queue")
            _seed_insiders(path, ("T01", "T02", "T03", "T04", "T05", "T06"))
            with self.assertRaises(SystemExit) as caught:
                main(["run", "--db", path, "--max-theses", "2"])
            self.assertEqual(caught.exception.code, 2)

            def choose_two(task):
                prompt = task["prompt"]
                if prompt.startswith("TASK: legate_selection"):
                    ids = _lead_ids(prompt)[:2]
                    return {"lead_ids": ids, "rationale": {str(item): "Kept for the shortlist." for item in ids}}
                return fixture_response(prompt)

            result = run_with_fixture(path, responder=choose_two, queue_dir=queue)
            report = Path(result["report"]).read_text(encoding="utf-8")
            candidates = Path(result["candidates"]).read_text(encoding="utf-8")
            conn = connect(path)
            try:
                ranked = conn.execute(
                    """
                    SELECT l.ticker, k.rank, k.presented
                    FROM rankings AS k
                    JOIN leads AS l ON l.lead_id = k.lead_id
                    ORDER BY k.rank
                    """
                ).fetchall()
                decisions = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
                picks = conn.execute(
                    "SELECT lead_id, rationale FROM legate_selections ORDER BY pick_order"
                ).fetchall()
            finally:
                conn.close()
        self.assertEqual(result["rankings"], 6)
        self.assertEqual(result["decisions"], 2)
        self.assertEqual(decisions, 2)
        self.assertEqual(len(ranked), 6)
        self.assertEqual(len(picks), 2)
        self.assertEqual(report.count("\n## Rank "), 2)
        self.assertEqual(candidates.count("\n## Rank "), 6)
        self.assertIn("2 selected theses.", report)
        self.assertIn("6 candidate theses.", candidates)
        self.assertIn("Kept for the shortlist.", report)
        presented = [row["ticker"] for row in ranked if row["presented"]]
        held = [row["ticker"] for row in ranked if not row["presented"]]
        self.assertEqual(len(presented), 2)
        self.assertEqual(len(held), 4)
        for ticker in presented:
            self.assertIn(ticker, report)
            self.assertIn(ticker, candidates)
            self.assertIn(f"https://www.sec.gov/Archives/edgar/data/1/{ticker.lower()}.xml", report)
        for ticker in held:
            self.assertIn(ticker, candidates)
            self.assertNotIn(ticker, report)
        self.assertIn("### Praetorian", report)
        self.assertIn("### Praetorian", candidates)
        self.assertIn("Form 4", report)

    def test_legate_selection_rejects_an_invalid_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "history.sqlite")
            queue = str(Path(tmp) / "agent_queue")
            _seed_insiders(path, ("T01", "T02", "T03", "T04", "T05", "T06"))

            def choose_all(task):
                prompt = task["prompt"]
                if prompt.startswith("TASK: legate_selection"):
                    ids = _lead_ids(prompt)
                    return {"lead_ids": ids, "rationale": {str(item): "All of them." for item in ids}}
                return fixture_response(prompt)

            with self.assertRaises(AgentResultError) as caught:
                run_with_fixture(path, responder=choose_all, queue_dir=queue)
            self.assertIn("at most 5", str(caught.exception))
            conn = connect(path)
            try:
                stored = conn.execute("SELECT run_id, status, stage FROM pipeline_runs").fetchone()
                self.assertEqual(stored["status"], "awaiting_agents")
                self.assertEqual(stored["stage"], "legate_selection")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM rankings").fetchone()[0], 6)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM rankings WHERE presented = 1").fetchone()[0], 0)
                run_id = stored["run_id"]
            finally:
                conn.close()
            result_path = Path(queue) / run_id / "legate_selection" / "legate_selection.result.json"
            task_path = result_path.with_name("legate_selection.task.json")
            ids = _lead_ids(json.loads(task_path.read_text(encoding="utf-8"))["prompt"])
            candidates = Path(tmp) / "reports" / f"{run_id}-candidates.md"
            self.assertTrue(candidates.is_file())
            self.assertEqual(candidates.read_text(encoding="utf-8").count("\n## Rank "), 6)
            self.assertFalse((Path(tmp) / "reports" / f"{run_id}-final.md").is_file())

            result_path.write_text(
                json.dumps({"lead_ids": [999999], "rationale": {"999999": "missing"}}) + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(AgentResultError) as caught:
                ingest_run(path, run_id, queue_dir=queue)
            self.assertIn("unknown lead_ids", str(caught.exception))

            result_path.write_text(
                json.dumps({"lead_ids": [ids[0], ids[0]], "rationale": {str(ids[0]): "twice"}}) + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(AgentResultError) as caught:
                ingest_run(path, run_id, queue_dir=queue)
            self.assertIn("duplicate", str(caught.exception))

            result_path.write_text(
                json.dumps({"lead_ids": [ids[0]], "rationale": {}}) + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(AgentResultError) as caught:
                ingest_run(path, run_id, queue_dir=queue)
            self.assertIn("rationale", str(caught.exception))

            chosen = ids[:2]
            result_path.write_text(
                json.dumps(
                    {"lead_ids": chosen, "rationale": {str(item): "Kept after the retry." for item in chosen}}
                )
                + "\n",
                encoding="utf-8",
            )
            done = ingest_run(path, run_id, queue_dir=queue)
            self.assertEqual(done["status"], "complete")
            self.assertEqual(done["decisions"], 2)
            self.assertEqual(done["rankings"], 6)
            final = Path(done["report"]).read_text(encoding="utf-8")
            self.assertEqual(final.count("\n## Rank "), 2)
            self.assertIn("Kept after the retry.", final)
            self.assertEqual(Path(done["candidates"]).read_text(encoding="utf-8").count("\n## Rank "), 6)

    def test_rejects_cli_finds_ticker_and_counterparty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "history.sqlite")
            _seed(path)
            run_with_fixture(path)
            stdout = io.StringIO()
            with _capture(stdout):
                code = main(["rejects", "--db", path, "--ticker", "scg"])
            self.assertEqual(code, 0)
            self.assertIn("ticker=SCG", stdout.getvalue())
            self.assertIn("stage=deterministic", stdout.getvalue())

            stdout = io.StringIO()
            with _capture(stdout):
                code = main(["rejects", "--db", path, "--counterparty", "Northwind Capital"])
            self.assertEqual(code, 0)
            self.assertIn("holder:Northwind Capital", stdout.getvalue())
            self.assertIn("ticker=NWT", stdout.getvalue())

            stdout = io.StringIO()
            with _capture(stdout):
                code = main(["rejects", "--db", path, "--counterparty", "AMZN"])
            self.assertEqual(code, 0)
            self.assertIn("buyer:Amazon (AMZN)", stdout.getvalue())

            with self.assertRaises(SystemExit):
                main(["rejects", "--db", path])

    def test_ingest_rejects_a_malformed_result_and_does_not_advance(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "history.sqlite")
            queue = str(Path(tmp) / "agent_queue")
            conn = _history(path)
            try:
                save_run(
                    conn,
                    "insider-buying",
                    "2026-10-12T16:00:00Z",
                    [
                        _figure("MSFT", "Microsoft", "insider_purchase", "ceo", "2026-10-01|Ada", 250000, "250000 shares", MSFT_URL),
                        *_caps("MSFT"),
                    ],
                )
            finally:
                conn.close()
            result = run_pipeline(path, queue_dir=queue)
            self.assertEqual(result["stage"], "significance")
            report = pipeline_status(path, result["run_id"])
            task = Path(report["outstanding"][0]["task"])
            task.with_name(task.name.replace(".task.json", ".result.json")).write_text(
                '{"magnitude": "huge"}\n',
                encoding="utf-8",
            )
            with self.assertRaises(AgentResultError) as caught:
                ingest_run(path, result["run_id"])
            self.assertIn("magnitude must be large, moderate, or small", str(caught.exception))
            again = pipeline_status(path, result["run_id"])
            self.assertEqual(again["status"], "awaiting_agents")
            self.assertEqual(again["stage"], "significance")
            conn = connect(path)
            try:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM leads WHERE run_id = ?", (result["run_id"],)).fetchone()[0],
                    0,
                )
            finally:
                conn.close()
            for relative in ("pipeline/cli.py", "pipeline/flow.py", "pipeline/__init__.py"):
                source = (REPO / relative).read_text(encoding="utf-8")
                self.assertNotIn("LLM_API_KEY", source)
                self.assertNotIn("LLM_BASE_URL", source)
                self.assertNotIn("LLM_MODEL", source)
                self.assertNotIn("fixture_response", source)
                self.assertNotIn("openai", source.casefold())

    def _assert_migration_once(self, conn):
        names = [row["name"] for row in conn.execute("SELECT name FROM schema_migrations")]
        self.assertEqual(names.count("002_lead_pipeline.sql"), 1)
        self.assertEqual(names.count("003_holder_role.sql"), 1)
        self.assertEqual(names.count("004_lead_counterparties.sql"), 1)
        self.assertEqual(names.count("005_deal_party_role.sql"), 1)
        self.assertEqual(names.count("006_agent_handoff.sql"), 1)
        self.assertEqual(names.count("008_macro_leads.sql"), 1)
        self.assertEqual(names.count("009_praetorian_flaws.sql"), 1)
        self.assertEqual(names.count("010_legate_selection.sql"), 1)
        self.assertNotIn("007_prices_cleanup.sql", names)
        tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertIn("lead_counterparties", tables)
        self.assertIn("legate_selections", tables)
        self.assertNotIn("prices", tables)
        self.assertNotIn("cleanup_log", tables)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(leads)")}
        for name in (
            "significance_magnitude",
            "significance_magnitude_rationale",
            "significance_novelty",
            "significance_novelty_rationale",
            "significance_attention",
            "significance_attention_rationale",
            "significance_timing",
            "significance_timing_rationale",
        ):
            self.assertIn(name, columns)
        indexes = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        self.assertIn("ix_lead_rejects_ticker", indexes)
        self.assertIn("ix_reject_cp_name", indexes)
        self.assertIn("ix_reject_cp_ticker", indexes)
        found = conn.execute(
            """
            SELECT r.scout, r.ticker, r.filing_date, r.rejection_stage, r.reason, r.source_url
            FROM lead_rejects AS r
            JOIN lead_reject_counterparties AS c ON c.reject_id = r.reject_id
            WHERE c.ticker = 'AMZN' AND c.role = 'buyer'
            """
        ).fetchall()
        self.assertTrue(found)
        self.assertIn("ZZZ", {row["ticker"] for row in found})

    def _assert_rejects(self, conn):
        rows = {row["ticker"]: row for row in conn.execute("SELECT * FROM lead_rejects")}
        self.assertEqual(rows["SCG"]["rejection_stage"], "deterministic")
        self.assertIn("50000000", rows["SCG"]["reason"])
        self.assertEqual(rows["SCG"]["significance_magnitude"], "small")
        self.assertIsNone(rows["SCG"]["significance_novelty"])
        self.assertNotIn("TINY", rows)
        tiny = conn.execute(
            """
            SELECT l.significance_novelty, l.significance_timing, t.assumptions, k.surviving_issues
            FROM leads AS l
            JOIN theses AS t ON t.lead_id = l.lead_id
            JOIN rankings AS k ON k.lead_id = l.lead_id
            WHERE l.ticker = 'TINY'
            """
        ).fetchone()
        self.assertEqual(tiny["significance_novelty"], "routine")
        self.assertEqual(tiny["significance_timing"], "early")
        self.assertIn("novelty routine", tiny["assumptions"])
        self.assertGreaterEqual(tiny["surviving_issues"], 1)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM praetorian_reviews").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM rankings").fetchone()[0],
        )
        self.assertEqual(rows["UNV"]["reason"], "unverified figure")
        self.assertIsNone(rows["UNV"]["significance_magnitude"])
        self.assertEqual(rows["NWT"]["significance_magnitude"], "small")
        self.assertEqual(rows["NWT"]["accession"], "0001234567-26-000009")
        amzn_reasons = [
            row["reason"]
            for row in conn.execute(
                "SELECT reason FROM lead_rejects WHERE ticker = 'AMZN' AND rejection_stage = 'deterministic'"
            )
        ]
        self.assertTrue(any("older period kept for growth comparison" in reason for reason in amzn_reasons))
        self.assertTrue(any("extra span" in reason for reason in amzn_reasons))
        lead_periods = [
            row["period"]
            for row in conn.execute(
                """
                SELECT f.period
                FROM leads AS l
                JOIN lead_figures AS link ON link.lead_id = l.lead_id
                JOIN figures AS f ON f.figure_id = link.figure_id
                WHERE l.ticker = 'AMZN' AND l.source_scout = 'capex_chain' AND f.ticker = 'AMZN'
                """
            )
        ]
        self.assertEqual(lead_periods, ["2026-07-01/2026-09-30"])

    def _assert_leads_and_theses(self, conn, prompts):
        pass_two = conn.execute("SELECT COUNT(*) FROM theses WHERE pass_number = 2").fetchone()[0]
        self.assertEqual(pass_two, 0)
        self.assertEqual(sum(1 for prompt in prompts if prompt.startswith("TASK: centurion") and "\nPASS: 2\n" in prompt), 0)
        chain_prompt = next(prompt for prompt in prompts if "TICKER: AMZN" in prompt and "CONSTITUENTS_BEGIN" in prompt)
        self.assertIn("what the spending is actually on", chain_prompt)
        self.assertIn("which customers placed the orders", chain_prompt)
        self.assertIn("MACRO: none", chain_prompt)
        self.assertIn("\nPASS: 1\n", chain_prompt)
        insider = next(prompt for prompt in prompts if "\nPASS: 1\n" in prompt and "LEAD_TYPE: insider_buying" in prompt)
        self.assertNotIn("CONSTITUENTS_BEGIN", insider)
        self.assertNotIn("MACRO:", insider)
        self.assertNotIn("RELATED_FINDINGS", insider)

        orcl = conn.execute(
            "SELECT parent_position_id, status FROM leads WHERE ticker = 'ORCL' AND source_url = ?",
            (ORCL_URL,),
        ).fetchone()
        self.assertIsNotNone(orcl["parent_position_id"])
        self.assertEqual(orcl["status"], "researched")
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM rankings AS k JOIN leads AS l ON l.lead_id = k.lead_id WHERE l.ticker = 'ORCL'").fetchone()[0],
            0,
        )
        reviewed = {
            row["source_scout"]
            for row in conn.execute(
                """
                SELECT l.source_scout
                FROM rankings AS k
                JOIN leads AS l ON l.lead_id = k.lead_id
                """
            )
        }
        self.assertEqual(reviewed, {"capex_chain", "insider-buying"})
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM praetorian_reviews").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM rankings").fetchone()[0],
        )

    def _assert_merge_rank_and_decisions(self, conn):
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM legate_merges").fetchone()[0], 0)
        survivor = conn.execute(
            "SELECT lead_id, ticker, source_scout, status, priority, summary FROM leads WHERE source_scout = 'capex_chain'"
        ).fetchone()
        self.assertIn(AMZN_URL, survivor["summary"])
        self.assertIn(GEV_URL, survivor["summary"])
        self.assertIn("Spender", survivor["summary"])
        self.assertIn("Supplier", survivor["summary"])
        self.assertEqual(survivor["ticker"], "AMZN")
        self.assertEqual(survivor["status"], "presented")
        self.assertEqual(survivor["priority"], 1)
        urls = {
            row["source_url"]
            for row in conn.execute(
                """
                SELECT f.source_url
                FROM lead_figures AS link
                JOIN figures AS f ON f.figure_id = link.figure_id
                WHERE link.lead_id = ?
                """,
                (survivor["lead_id"],),
            )
        }
        self.assertEqual(urls, {AMZN_URL, GEV_URL})
        thesis = conn.execute(
            "SELECT price_run, recommendation, macro_context, pass_number FROM theses WHERE lead_id = ?",
            (survivor["lead_id"],),
        ).fetchone()
        self.assertEqual(thesis["pass_number"], 1)
        self.assertEqual(thesis["price_run"], "acceleration")
        self.assertEqual(thesis["recommendation"], "GEV")
        self.assertIsNone(thesis["macro_context"])
        self.assertNotIn(
            "Macro context used:",
            conn.execute("SELECT assumptions FROM theses WHERE lead_id = ?", (survivor["lead_id"],)).fetchone()["assumptions"],
        )
        decisions = conn.execute("SELECT status FROM decisions").fetchall()
        self.assertEqual([row["status"] for row in decisions], ["pending", "pending", "pending"])
        rank = conn.execute(
            """
            SELECT l.source_scout FROM rankings AS k
            JOIN leads AS l ON l.lead_id = k.lead_id
            WHERE k.rank = 1
            """
        ).fetchone()
        self.assertEqual(rank["source_scout"], "capex_chain")
        tokens = conn.execute("SELECT tokens_used, token_budget, provider, status, stage FROM pipeline_runs").fetchone()
        self.assertEqual(tokens["provider"], "agent")
        self.assertEqual(tokens["tokens_used"], 0)
        self.assertEqual(tokens["token_budget"], 0)
        self.assertEqual(tokens["status"], "complete")
        self.assertEqual(tokens["stage"], "legate_selection")


def _lead_ids(prompt):
    found = []
    for line in prompt.splitlines():
        if line.startswith("LEAD_ID: "):
            found.append(int(line.split(":", 1)[1].strip()))
    return found


def _seed_insiders(path, tickers):
    conn = _history(path)
    try:
        rows = []
        for ticker in tickers:
            rows.append(
                _figure(
                    ticker,
                    ticker,
                    "insider_purchase",
                    "ceo",
                    "2026-10-01|Ada",
                    250000,
                    "250000 shares",
                    f"https://www.sec.gov/Archives/edgar/data/1/{ticker.lower()}.xml",
                )
            )
        rows.extend(_caps(*tickers))
        save_run(conn, "insider-buying", "2026-10-12T16:10:00Z", rows)
    finally:
        conn.close()


def _seed(path):
    conn = _history(path)
    try:
        capex = save_run(
            conn,
            "capex",
            "2026-10-12T14:30:52Z",
            [
                _figure("AMZN", "Amazon", "PaymentsToAcquireProductiveAssets", "quarter", "2026-07-01/2026-09-30", 5_000_000_000, "5000000000 supplier: Globex Power", AMZN_URL),
                _figure("AMZN", "Amazon", "PaymentsToAcquirePropertyPlantAndEquipment", "quarter", "2025-07-01/2025-09-30", 1_000_000_000, "1000000000", AMZN_URL),
                _figure("AMZN", "Amazon", "PaymentsToAcquirePropertyPlantAndEquipment", "quarter", "2024-07-01/2024-09-30", 800_000_000, "800000000", AMZN_URL),
                _figure("AMZN", "Amazon", "PaymentsToAcquirePropertyPlantAndEquipment", "ttm", "2025-10-01/2026-09-30", 9_000_000_000, "9000000000", AMZN_URL),
                _figure("UNV", "Unverified Co", "PaymentsToAcquireProductiveAssets", "quarter", "2025-01-01/2025-03-31", 3_000_000_000, "3000000000", "https://www.sec.gov/Archives/edgar/data/1/unv.htm", verified=0),
                _figure("ORCL", "Oracle", "PaymentsToAcquireProductiveAssets", "quarter", "2026-07-01/2026-09-30", 2_000_000_000, "2000000000", ORCL_URL),
                _figure("ORCL", "Oracle", "PaymentsToAcquirePropertyPlantAndEquipment", "quarter", "2025-07-01/2025-09-30", 500_000_000, "500000000", ORCL_PRIOR),
                *_caps("AMZN", "ORCL", "GEV", "MSFT", "TINY"),
            ],
        )
        save_run(
            conn,
            "orders-backlog",
            "2026-10-12T14:31:00Z",
            [
                _figure("GEV", "Globex Power", "supplier_order", "Amazon", "2026-10-01", 2_000_000_000, "2000000000 buyer: Amazon", GEV_URL),
                _figure("GEV", "Globex Power", "customer_backlog", "RevenueRemainingPerformanceObligation", "2026-06-30|2026-07-22", 40_000_000, "40000000", GEV_URL),
                _figure("ZZZ", "Small Supplier", "supplier_order", "Amazon", "2026-10-01", None, "buyer: Amazon", "https://www.sec.gov/Archives/edgar/data/9/zzz.htm"),
            ],
        )
        save_run(
            conn,
            "insider-buying",
            "2026-10-12T14:32:00Z",
            [
                _figure("MSFT", "Microsoft", "insider_purchase", "ceo", "2026-10-01|Buyer", 250000, "250000 shares", MSFT_URL),
                _figure("TINY", "Tiny Co", "insider_purchase", "", "2026-10-01|Buyer", 20000, "low-signal 20000", "https://www.sec.gov/Archives/edgar/data/8/tiny.xml"),
            ],
        )
        save_run(
            conn,
            "debt-raise",
            "2026-10-12T14:33:00Z",
            [
                _figure(
                    "SCG",
                    "Scentre",
                    "facility_amount",
                    "facility",
                    "2026-10-01 0000320193-26-000001",
                    1_000_000,
                    "1000000",
                    "https://www.sec.gov/Archives/edgar/data/320193/scg.htm",
                    currency="USD",
                )
            ],
        )
        save_run(
            conn,
            "accumulation",
            "2026-10-12T14:34:00Z",
            [
                _figure(
                    "NWT",
                    "North West",
                    "substantial_holding",
                    "SC 13D",
                    "2026-10-01|0001234567-26-000009|Northwind Capital",
                    2,
                    "2%",
                    "https://www.sec.gov/Archives/edgar/data/7/nwt.htm",
                )
            ],
        )
    finally:
        conn.close()
    pipeline = connect(path)
    try:
        position = lodge_lead(
            pipeline,
            "capex",
            "ORCL",
            "US",
            "capex",
            capex,
            company="Oracle",
            source_url=ORCL_PRIOR,
            summary="open position",
        )
        pipeline.execute(
            "UPDATE leads SET status = 'acted', updated_at = '2026-10-11T00:00:00Z' WHERE lead_id = ?",
            (position,),
        )
        pipeline.commit()
    finally:
        pipeline.close()


def _history(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    from shared.store import ensure_history

    ensure_history(conn)
    return conn


def _caps(*tickers):
    rows = []
    for ticker in tickers:
        rows.append(
            _figure(
                ticker,
                ticker,
                "EntityCommonStockSharesOutstanding",
                "shares",
                "2026-10-01",
                1_000_000_000,
                "1000000000",
                f"https://www.sec.gov/Archives/edgar/data/1/{ticker.lower()}-shares.htm",
            )
        )
        rows.append(
            _figure(
                ticker,
                ticker,
                "share_price",
                "close",
                "2026-10-01",
                10,
                "10",
                f"https://www.sec.gov/Archives/edgar/data/1/{ticker.lower()}-price.htm",
            )
        )
    return rows


def _figure(ticker, company, figure_type, instrument, period, value, value_text, source_url, verified=1, currency="USD"):
    return {
        "ticker": ticker,
        "company": company,
        "figure_type": figure_type,
        "instrument": instrument,
        "period": period,
        "value": value,
        "value_text": value_text,
        "currency": currency,
        "seen_at": "2026-10-12",
        "announced_on": "2026-10-01",
        "source_url": source_url,
        "source_type": "filing",
        "verified": verified,
    }


class _Response:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _capture:
    def __init__(self, buffer):
        self.buffer = buffer

    def __enter__(self):
        self._stdout = sys.stdout
        sys.stdout = self.buffer
        return self.buffer

    def __exit__(self, *_args):
        sys.stdout = self._stdout
        return False


if __name__ == "__main__":
    unittest.main()
