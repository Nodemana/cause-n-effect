"""Full sweep against fixture figures. The stub provider never opens a socket."""

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

from pipeline.cli import main  # noqa: E402
from pipeline.flow import run_pipeline  # noqa: E402
from pipeline.llm import (  # noqa: E402
    OpenAICompatibleProvider,
    StubProvider,
    resolve_provider,
)
from shared.pipeline_store import connect, lodge_lead  # noqa: E402
from shared.store import save_run  # noqa: E402

AMZN_URL = "https://www.sec.gov/Archives/edgar/data/1018724/000101872426000001/amzn.htm"
GEV_URL = "https://www.sec.gov/Archives/edgar/data/2088000/000208800026000001/gev.htm"
MSFT_URL = "https://www.sec.gov/Archives/edgar/data/789019/000078901926000001/msft.xml"
ORCL_URL = "https://www.sec.gov/Archives/edgar/data/1341439/000134143926000001/orcl.htm"
ORCL_PRIOR = "https://www.sec.gov/Archives/edgar/data/1341439/000134143925000001/prior.htm"


class RecordingProvider:
    def __init__(self):
        self.inner = StubProvider()
        self.name = self.inner.name
        self.prompts = []

    def complete(self, prompt, *, max_tokens):
        self.prompts.append(prompt)
        return self.inner.complete(prompt, max_tokens=max_tokens)


class PipelineFlowTests(unittest.TestCase):
    def setUp(self):
        self._saved_key = os.environ.pop("LLM_API_KEY", None)

    def tearDown(self):
        if self._saved_key is not None:
            os.environ["LLM_API_KEY"] = self._saved_key

    def test_full_sweep_with_stub(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "history.sqlite")
            _seed(path)
            provider = RecordingProvider()
            result = run_pipeline(path, provider=provider, token_budget=80000)
            self.assertEqual(result["provider"], "stub")
            self.assertGreater(result["tokens_used"], 0)
            self.assertLessEqual(result["tokens_used"], 80000)
            self.assertEqual(result["macro"], "skipped")
            self.assertFalse(result["dry_run"])
            self.assertEqual(result["merges"], 1)
            self.assertEqual(result["decisions"], 2)
            self.assertEqual(result["rankings"], 2)

            conn = connect(path)
            try:
                self._assert_migration_once(conn)
                self._assert_rejects(conn)
                self._assert_leads_and_theses(conn, provider)
                self._assert_merge_rank_and_decisions(conn)
                before = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
            finally:
                conn.close()

            again = run_pipeline(path, provider=StubProvider(), token_budget=80000)
            conn = connect(path)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0], before)
                self.assertGreater(again["rejects"], 0)
            finally:
                conn.close()

    def test_priced_in_and_macro_cache_on_the_return_pass_only(self):
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
                            "PaymentsToAcquireProductiveAssets",
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
                            "supplier_order",
                            "Amazon",
                            "2026-07-03",
                            500_000_000,
                            "500000000",
                            GEV_URL + "#prior",
                        ),
                    ],
                )
            finally:
                conn.close()
            run_pipeline(path, provider=StubProvider(), macro_cache=str(cache), token_budget=80000)
            conn = connect(path)
            try:
                merged = conn.execute(
                    """
                    SELECT t.price_run, t.macro_context, t.assumptions, t.pass_number, l.ticker
                    FROM theses AS t
                    JOIN leads AS l ON l.lead_id = t.lead_id
                    WHERE t.pass_number = 2
                    """
                ).fetchone()
                self.assertEqual(merged["price_run"], "priced-in")
                self.assertEqual(merged["macro_context"], "Policy rates unchanged.")
                self.assertIn("Macro context used: Policy rates unchanged.", merged["assumptions"])
                first = conn.execute(
                    "SELECT price_run, macro_context FROM theses WHERE pass_number = 1"
                ).fetchall()
                self.assertTrue(first)
                for row in first:
                    self.assertIsNone(row["price_run"])
                    self.assertIsNone(row["macro_context"])
                review = conn.execute(
                    """
                    SELECT r.surviving_issues, r.assumptions_that_fail,
                           r.price_run_stance, r.price_run_counterevidence
                    FROM praetorian_reviews AS r
                    JOIN theses AS t ON t.thesis_id = r.thesis_id
                    WHERE t.pass_number = 2
                    """
                ).fetchone()
                self.assertGreaterEqual(review["surviving_issues"], 1)
                self.assertIn("priced-in", review["assumptions_that_fail"])
                self.assertEqual(review["price_run_stance"], "agree")
                self.assertIn("priced-in", review["price_run_counterevidence"])
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
                            "PaymentsToAcquireProductiveAssets",
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
                            "supplier_order",
                            "Amazon",
                            "2026-07-03",
                            500_000_000,
                            "500000000",
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
            run_pipeline(path, provider=StubProvider(), macro_cache=str(cache), token_budget=80000)
            conn = connect(path)
            try:
                merged = conn.execute(
                    "SELECT macro_context, assumptions FROM theses WHERE pass_number = 2"
                ).fetchone()
                self.assertIn("DGS10", merged["macro_context"])
                self.assertIn("4.2", merged["macro_context"])
                self.assertIn("2026-10-01", merged["macro_context"])
                self.assertNotIn("2026-09-01", merged["macro_context"])
                self.assertNotIn("JSON cache should not be used.", merged["macro_context"])
                self.assertIn("Macro context used:", merged["assumptions"])
                self.assertNotIn("JSON cache should not be used.", merged["assumptions"])
                review = conn.execute(
                    """
                    SELECT price_run_stance, price_run_counterevidence
                    FROM praetorian_reviews AS r
                    JOIN theses AS t ON t.thesis_id = r.thesis_id
                    WHERE t.pass_number = 2
                    """
                ).fetchone()
                self.assertEqual(review["price_run_stance"], "agree")
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
                                "PaymentsToAcquireProductiveAssets",
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
                                "supplier_order",
                                "Amazon",
                                "2026-07-03",
                                500_000_000,
                                "500000000",
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
                result = run_pipeline(path, provider=StubProvider(), token_budget=80000)
                conn = connect(path)
                try:
                    merged = conn.execute(
                        "SELECT macro_context FROM theses WHERE pass_number = 2"
                    ).fetchone()
                    first = conn.execute(
                        "SELECT macro_context FROM theses WHERE pass_number = 1"
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
        self.assertTrue(first)
        for row in first:
            self.assertIsNone(row["macro_context"])

    def test_token_budget_stops_before_the_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "history.sqlite")
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
            provider = RecordingProvider()
            result = run_pipeline(path, provider=provider, token_budget=0)
            self.assertEqual(provider.prompts, [])
            self.assertEqual(result["tokens_used"], 0)
            self.assertEqual(result["leads"], 0)
            conn = connect(path)
            try:
                row = conn.execute("SELECT rejection_stage, reason, significance_magnitude FROM lead_rejects").fetchone()
                self.assertEqual(row["rejection_stage"], "llm")
                self.assertEqual(row["reason"], "token budget exhausted")
                self.assertIsNone(row["significance_magnitude"])
            finally:
                conn.close()

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "history.sqlite")
            _seed(path)
            result = run_pipeline(path, provider=StubProvider(), dry_run=True, token_budget=80000)
            self.assertTrue(result["dry_run"])
            self.assertIsNone(result["run_id"])
            self.assertGreater(result["leads"], 0)
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
        self.assertIn("token-budget", completed.stdout)
        self.assertIn("LLM_API_KEY", completed.stdout)

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
        finally:
            socket.socket = real_socket
            urllib.request.urlopen = real_open

    def test_rejects_cli_finds_ticker_and_counterparty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "history.sqlite")
            _seed(path)
            run_pipeline(path, provider=StubProvider(), token_budget=80000)
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
            self.assertIn("buyer:Northwind Capital", stdout.getvalue())
            self.assertIn("ticker=NWT", stdout.getvalue())

            stdout = io.StringIO()
            with _capture(stdout):
                code = main(["rejects", "--db", path, "--counterparty", "AMZN"])
            self.assertEqual(code, 0)
            self.assertIn("buyer:Amazon (AMZN)", stdout.getvalue())

            with self.assertRaises(SystemExit):
                main(["rejects", "--db", path])

    def test_http_provider_sends_the_key_and_does_not_record_it(self):
        secret = "test-key"
        captured = {}

        def opener(request, timeout=0):
            captured["auth"] = request.get_header("Authorization")
            captured["timeout"] = timeout
            body = json.dumps(
                {
                    "choices": [{"message": {"content": "{\"ok\": true}"}}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 4},
                }
            ).encode("utf-8")
            return _Response(body)

        provider = resolve_provider(
            {"LLM_API_KEY": secret, "LLM_BASE_URL": "https://llm.example/v1", "LLM_MODEL": "demo-model"}
        )
        self.assertIsInstance(provider, OpenAICompatibleProvider)
        self.assertNotIn(secret, repr(provider))
        provider._opener = opener
        result = provider.complete("hello", max_tokens=8)
        self.assertEqual(result.text, '{"ok": true}')
        self.assertEqual(result.prompt_tokens, 3)
        self.assertEqual(result.completion_tokens, 4)
        self.assertEqual(captured["auth"], "Bearer " + secret)
        self.assertNotIn(secret, repr(provider))
        self.assertIsInstance(resolve_provider({}), StubProvider)

    def _assert_migration_once(self, conn):
        names = [row["name"] for row in conn.execute("SELECT name FROM schema_migrations")]
        self.assertEqual(names.count("002_lead_pipeline.sql"), 1)
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
        self.assertEqual(rows["TINY"]["rejection_stage"], "llm")
        self.assertEqual(rows["TINY"]["significance_novelty"], "routine")
        self.assertEqual(rows["TINY"]["significance_attention"], "unnoticed")
        self.assertEqual(rows["TINY"]["significance_timing"], "early")
        self.assertTrue(rows["TINY"]["significance_magnitude_rationale"])
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
                WHERE l.ticker = 'AMZN' AND l.source_scout = 'capex'
                """
            )
        ]
        self.assertEqual(lead_periods, ["2026-07-01/2026-09-30"])

    def _assert_leads_and_theses(self, conn, provider):
        pass_two = conn.execute("SELECT COUNT(*) FROM theses WHERE pass_number = 2").fetchone()[0]
        self.assertEqual(pass_two, 1)
        prompts = provider.prompts
        self.assertEqual(sum(1 for prompt in prompts if prompt.startswith("TASK: centurion") and "\nPASS: 2\n" in prompt), 1)
        return_pass = next(prompt for prompt in prompts if "\nPASS: 2\n" in prompt)
        self.assertIn("what the spending is actually on", return_pass)
        self.assertIn("which customers placed the orders", return_pass)
        self.assertIn("MACRO: none", return_pass)
        self.assertNotIn("CONSTITUENTS_BEGIN", next(prompt for prompt in prompts if "\nPASS: 1\n" in prompt and "LEAD_TYPE: insider_buying" in prompt))
        first_capex = next(prompt for prompt in prompts if "\nPASS: 1\n" in prompt and "LEAD_TYPE: capex" in prompt and "TICKER: AMZN" in prompt)
        self.assertIn("what the spending is actually on", first_capex)
        self.assertNotIn("PASS: 2", first_capex)

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
                SELECT l.source_scout, l.ticker, t.pass_number
                FROM praetorian_reviews AS r
                JOIN theses AS t ON t.thesis_id = r.thesis_id
                JOIN leads AS l ON l.lead_id = t.lead_id
                """
            )
        }
        self.assertEqual(reviewed, {"legate", "insider-buying"})

    def _assert_merge_rank_and_decisions(self, conn):
        merge = conn.execute("SELECT survivor_lead_id, rationale, evidence_refs, priority_rank FROM legate_merges").fetchone()
        self.assertIn(AMZN_URL, merge["rationale"])
        self.assertIn(GEV_URL, merge["rationale"])
        self.assertIn("sector match is not the link", merge["rationale"])
        evidence = json.loads(merge["evidence_refs"])
        urls = {item["source_url"] for item in evidence}
        self.assertEqual(urls, {AMZN_URL, GEV_URL})
        members = {
            row["lead_id"]
            for row in conn.execute("SELECT lead_id FROM legate_merge_leads WHERE merge_id = (SELECT merge_id FROM legate_merges)")
        }
        self.assertNotIn(merge["survivor_lead_id"], members)
        self.assertEqual(len(members), 2)
        survivor = conn.execute("SELECT ticker, source_scout, status, priority FROM leads WHERE lead_id = ?", (merge["survivor_lead_id"],)).fetchone()
        self.assertEqual(survivor["ticker"], "AMZN")
        self.assertEqual(survivor["source_scout"], "legate")
        self.assertEqual(survivor["status"], "presented")
        self.assertEqual(survivor["priority"], 1)
        thesis = conn.execute(
            "SELECT price_run, recommendation, macro_context, pass_number FROM theses WHERE lead_id = ?",
            (merge["survivor_lead_id"],),
        ).fetchone()
        self.assertEqual(thesis["pass_number"], 2)
        self.assertEqual(thesis["price_run"], "acceleration")
        self.assertEqual(thesis["recommendation"], "GEV")
        self.assertIsNone(thesis["macro_context"])
        self.assertNotIn(
            "Macro context used:",
            conn.execute("SELECT assumptions FROM theses WHERE lead_id = ?", (merge["survivor_lead_id"],)).fetchone()["assumptions"],
        )
        decisions = conn.execute("SELECT status FROM decisions").fetchall()
        self.assertEqual([row["status"] for row in decisions], ["pending", "pending"])
        rank = conn.execute(
            """
            SELECT l.source_scout FROM rankings AS k
            JOIN leads AS l ON l.lead_id = k.lead_id
            WHERE k.rank = 1
            """
        ).fetchone()
        self.assertEqual(rank["source_scout"], "legate")
        tokens = conn.execute("SELECT tokens_used, token_budget, provider FROM pipeline_runs").fetchone()
        self.assertEqual(tokens["provider"], "stub")
        self.assertGreater(tokens["tokens_used"], 0)
        self.assertLessEqual(tokens["tokens_used"], tokens["token_budget"])


def _seed(path):
    conn = _history(path)
    try:
        capex = save_run(
            conn,
            "capex",
            "2026-10-12T14:30:52Z",
            [
                _figure("AMZN", "Amazon", "PaymentsToAcquireProductiveAssets", "quarter", "2026-07-01/2026-09-30", 5_000_000_000, "5000000000 supplier: Globex Power", AMZN_URL),
                _figure("AMZN", "Amazon", "PaymentsToAcquireProductiveAssets", "quarter", "2025-07-01/2025-09-30", 1_000_000_000, "1000000000", AMZN_URL),
                _figure("AMZN", "Amazon", "PaymentsToAcquireProductiveAssets", "ttm", "2025-10-01/2026-09-30", 9_000_000_000, "9000000000", AMZN_URL),
                _figure("UNV", "Unverified Co", "PaymentsToAcquireProductiveAssets", "quarter", "2025-01-01/2025-03-31", 3_000_000_000, "3000000000", "https://www.sec.gov/Archives/edgar/data/1/unv.htm", verified=0),
                _figure("ORCL", "Oracle", "PaymentsToAcquireProductiveAssets", "quarter", "2026-07-01/2026-09-30", 2_000_000_000, "2000000000", ORCL_URL),
                _figure("ORCL", "Oracle", "PaymentsToAcquireProductiveAssets", "quarter", "2025-07-01/2025-09-30", 500_000_000, "500000000", ORCL_PRIOR),
                *_caps("AMZN", "ORCL", "GEV", "MSFT", "TINY"),
            ],
        )
        save_run(
            conn,
            "orders-backlog",
            "2026-10-12T14:31:00Z",
            [
                _figure("GEV", "Globex Power", "supplier_order", "Amazon", "2026-10-01", 2_000_000_000, "2000000000 buyer: Amazon", GEV_URL),
                _figure("GEV", "Globex Power", "supplier_order", "Amazon", "2026-07-03", 500_000_000, "500000000", GEV_URL),
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
