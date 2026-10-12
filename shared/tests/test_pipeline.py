"""Pipeline migration: fresh database, legacy leads, position updates, scores."""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from shared.pipeline_store import (  # noqa: E402
    LEAD_TYPES,
    MIGRATION_NAME,
    MIGRATION_PATH,
    compute_quality_score,
    connect,
    ensure_pipeline,
    lodge_lead,
    record_criterion_result,
)

NEIGHBOR_SCHEMA = """
CREATE TABLE debt_scout_runs (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
    started_at TEXT NOT NULL,
    threshold REAL NOT NULL,
    filings_seen INTEGER NOT NULL,
    resolved_by_cheap_filter INTEGER NOT NULL,
    sent_to_full_document INTEGER NOT NULL,
    score_histogram TEXT NOT NULL
);

CREATE TABLE debt_scout_filing_scores (
    score_id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    accession TEXT NOT NULL,
    ticker TEXT,
    score REAL,
    stage_reached TEXT NOT NULL CHECK (stage_reached IN ('cheap_filter', 'full_document')),
    form TEXT,
    cheap_score REAL,
    decided INTEGER NOT NULL,
    flags TEXT NOT NULL
);

CREATE TABLE scout_tests (
    test_id INTEGER PRIMARY KEY,
    scout TEXT NOT NULL,
    note TEXT NOT NULL
);

CREATE TABLE schema_migrations (
    name TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);
"""


class PipelineMigrationTests(unittest.TestCase):
    def test_fresh_database_seeds_rubrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(str(Path(tmp) / "fresh.sqlite"))
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(leads)")}
            self.assertIn("lead_id", cols)
            self.assertIn("outcome", cols)
            self.assertIn("parent_position_id", cols)
            self.assertNotIn("rank", cols)
            self.assertNotIn("rationale", cols)
            types = [row["lead_type"] for row in conn.execute("SELECT lead_type FROM rubrics ORDER BY lead_type")]
            self.assertEqual(types, sorted(LEAD_TYPES))
            fixed = [row["fixed"] for row in conn.execute("SELECT fixed FROM rubrics")]
            self.assertEqual(fixed, [1, 1, 1, 1, 1, 1])
            notes = conn.execute(
                "SELECT notes FROM rubrics WHERE lead_type = 'capex'"
            ).fetchone()["notes"]
            self.assertIn("configurable default", notes.casefold())
            self._assert_threshold(conn, "insider_buying", "insider_buy_dollars", 10000, "USD")
            self._assert_threshold(conn, "debt_raise", "raise_vs_company", 5, "percent")
            self._assert_threshold(conn, "orders_backlog", "backlog_qoq_growth", 10, "percent")
            self._assert_threshold(conn, "accumulation", "min_stake_pct", 5, "percent")
            self._assert_threshold(conn, "capex", "min_market_cap", 2000000000, "USD")
            qualitative = conn.execute(
                """
                SELECT configurable, threshold_value
                FROM rubric_criteria c
                JOIN rubrics r ON r.rubric_id = c.rubric_id
                WHERE r.lead_type = 'capex' AND c.name = 'official_source'
                """
            ).fetchone()
            self.assertEqual(qualitative["configurable"], 0)
            self.assertIsNone(qualitative["threshold_value"])
            rubric_id = conn.execute(
                "SELECT rubric_id FROM rubrics WHERE lead_type = 'capex'"
            ).fetchone()["rubric_id"]
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE rubric_criteria SET threshold_value = 1 WHERE rubric_id = ?",
                    (rubric_id,),
                )
            conn.executescript(MIGRATION_PATH.read_text(encoding="utf-8"))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM rubrics").fetchone()[0], 6)
            again = connect(str(Path(tmp) / "fresh.sqlite"))
            names = [row["name"] for row in again.execute("SELECT name FROM schema_migrations")]
            self.assertEqual(names.count(MIGRATION_NAME), 1)
            self.assertEqual(again.execute("SELECT COUNT(*) FROM rubrics").fetchone()[0], 6)
            again.close()
            conn.close()

    def test_upgrades_existing_schema_and_keeps_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "legacy.sqlite")
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            schema = (REPO / "shared" / "schema.sql").read_text(encoding="utf-8")
            self.assertIn("PRIMARY KEY(run_id,ticker)", schema)
            conn.executescript(schema)
            conn.executescript(NEIGHBOR_SCHEMA)
            conn.execute(
                "INSERT INTO schema_migrations (name, applied_at) VALUES ('shared_schema.sql', '2026-01-01T00:00:00Z')"
            )
            conn.execute(
                "INSERT INTO schema_migrations (name, applied_at) VALUES ('001_history.sql', '2026-01-01T00:00:00Z')"
            )
            conn.execute(
                "INSERT INTO runs (run_id, scout, started_at) VALUES ('2026-02-01-debt-raise', 'debt-raise', '2026-02-01T00:00:00Z')"
            )
            conn.execute(
                "INSERT INTO runs (run_id, scout, started_at) VALUES ('2026-02-02-capex', 'capex', '2026-02-02T00:00:00Z')"
            )
            conn.execute(
                """
                INSERT INTO leads (run_id, ticker, rank, status, rationale)
                VALUES ('2026-02-01-debt-raise', 'SCG', 1, 'partly', 'named project facility')
                """
            )
            conn.execute(
                """
                INSERT INTO leads (run_id, ticker, rank, status, rationale)
                VALUES ('2026-02-02-capex', 'AMZN', NULL, NULL, NULL)
                """
            )
            conn.execute(
                """
                INSERT INTO figures (
                    run_id, ticker, figure_type, value, seen_at, verified
                ) VALUES ('2026-02-02-capex', 'AMZN', 'capex', 100.0, '2026-02-02T00:00:00Z', 1)
                """
            )
            conn.execute(
                """
                INSERT INTO debt_scout_runs (
                    run_id, started_at, threshold, filings_seen,
                    resolved_by_cheap_filter, sent_to_full_document, score_histogram
                ) VALUES ('2026-02-01-debt-raise', '2026-02-01T00:00:00Z', 2, 1, 1, 0, '{}')
                """
            )
            conn.execute(
                """
                INSERT INTO debt_scout_filing_scores (
                    run_id, accession, ticker, score, stage_reached, form,
                    cheap_score, decided, flags
                ) VALUES (
                    '2026-02-01-debt-raise', '0000000000-26-000001', 'SCG',
                    3, 'cheap_filter', '8-K', 3, 1, '[]'
                )
                """
            )
            conn.execute(
                "INSERT INTO scout_tests (scout, note) VALUES ('capex', 'fixture')"
            )
            conn.commit()

            ensure_pipeline(conn)

            leads = {
                row["ticker"]: row
                for row in conn.execute("SELECT * FROM leads ORDER BY ticker")
            }
            self.assertEqual(set(leads), {"AMZN", "SCG"})
            scg = leads["SCG"]
            self.assertEqual(scg["lead_type"], "debt_raise")
            self.assertEqual(scg["source_scout"], "debt-raise")
            self.assertEqual(scg["priority"], 1)
            self.assertEqual(scg["status"], "fresh")
            self.assertIsNone(scg["outcome"])
            self.assertIsNone(scg["quality_score"])
            self.assertIsNone(scg["parent_position_id"])
            self.assertEqual(scg["summary"], "named project facility\nlegacy status: partly")
            self.assertEqual(scg["created_at"], "2026-02-01T00:00:00Z")
            self.assertEqual(scg["run_id"], "2026-02-01-debt-raise")
            amzn = leads["AMZN"]
            self.assertEqual(amzn["lead_type"], "capex")
            self.assertEqual(amzn["source_scout"], "capex")
            self.assertIsNone(amzn["priority"])
            self.assertIsNone(amzn["summary"])
            self.assertEqual(amzn["status"], "fresh")
            self.assertEqual(
                conn.execute("SELECT value FROM figures").fetchone()["value"],
                100.0,
            )
            self.assertEqual(
                conn.execute("SELECT ticker FROM changes").fetchone()["ticker"],
                "AMZN",
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM debt_scout_runs").fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute("SELECT ticker FROM debt_scout_filing_scores").fetchone()["ticker"],
                "SCG",
            )
            self.assertEqual(
                conn.execute("SELECT note FROM scout_tests").fetchone()["note"],
                "fixture",
            )
            self.assertEqual(
                conn.execute(
                    "SELECT applied_at FROM schema_migrations WHERE name = 'shared_schema.sql'"
                ).fetchone()["applied_at"],
                "2026-01-01T00:00:00Z",
            )
            self.assertEqual(
                conn.execute(
                    "SELECT applied_at FROM schema_migrations WHERE name = '001_history.sql'"
                ).fetchone()["applied_at"],
                "2026-01-01T00:00:00Z",
            )
            self.assertIsNotNone(
                conn.execute(
                    "SELECT 1 FROM schema_migrations WHERE name = ?",
                    (MIGRATION_NAME,),
                ).fetchone()
            )
            self.assertFalse(
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name = 'leads_legacy'"
                ).fetchone()
            )
            ensure_pipeline(conn)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM rubrics").fetchone()[0], 6)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM scout_tests").fetchone()[0], 1)
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO leads (
                        lead_type, ticker, source_scout, run_id, status, created_at, updated_at
                    ) VALUES (
                        'capex', 'MSFT', 'capex', '2026-02-02-capex', 'underappreciated',
                        '2026-02-02T00:00:00Z', '2026-02-02T00:00:00Z'
                    )
                    """
                )
            conn.close()

    def test_rebuilds_leads_that_only_have_run_and_ticker(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "thin.sqlite")
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            conn.execute(
                """
                CREATE TABLE runs(
                  run_id TEXT PRIMARY KEY,
                  scout TEXT NOT NULL,
                  started_at TEXT NOT NULL,
                  notes TEXT)
                """
            )
            conn.execute(
                """
                CREATE TABLE leads(
                  run_id TEXT NOT NULL REFERENCES runs(run_id),
                  ticker TEXT NOT NULL,
                  PRIMARY KEY(run_id, ticker))
                """
            )
            conn.execute(
                "INSERT INTO runs (run_id, scout, started_at) VALUES ('2026-04-01-accumulation', 'accumulation', '2026-04-01T00:00:00Z')"
            )
            conn.execute(
                "INSERT INTO leads (run_id, ticker) VALUES ('2026-04-01-accumulation', 'NWT')"
            )
            conn.commit()
            ensure_pipeline(conn)
            row = conn.execute("SELECT * FROM leads").fetchone()
            self.assertEqual(row["ticker"], "NWT")
            self.assertEqual(row["lead_type"], "accumulation")
            self.assertEqual(row["source_scout"], "accumulation")
            self.assertEqual(row["status"], "fresh")
            self.assertIsNone(row["priority"])
            self.assertIsNone(row["summary"])
            self.assertIsNone(row["outcome"])
            conn.close()

    def test_position_update_and_quality_score(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(str(Path(tmp) / "score.sqlite"))
            conn.execute(
                "INSERT INTO runs (run_id, scout, started_at) VALUES ('2026-03-01-capex', 'capex', '2026-03-01T00:00:00Z')"
            )
            conn.execute(
                "INSERT INTO runs (run_id, scout, started_at) VALUES ('2026-03-02-centurion', 'centurion', '2026-03-02T00:00:00Z')"
            )
            conn.execute(
                "INSERT INTO runs (run_id, scout, started_at) VALUES ('2026-03-03-praetorian', 'praetorian', '2026-03-03T00:00:00Z')"
            )
            conn.commit()
            figure_id = conn.execute(
                """
                INSERT INTO figures (run_id, ticker, figure_type, seen_at, verified)
                VALUES ('2026-03-01-capex', 'AMZN', 'capex', '2026-03-01T00:00:00Z', 1)
                """
            ).lastrowid
            conn.commit()
            position_id = lodge_lead(
                conn,
                "capex",
                "AMZN",
                "NASDAQ",
                "capex",
                "2026-03-01-capex",
                figure_ids=[figure_id, figure_id],
                summary="spend up",
            )
            conn.execute(
                """
                UPDATE leads
                SET status = 'position', updated_at = '2026-03-03T00:00:00Z'
                WHERE lead_id = ?
                """,
                (position_id,),
            )
            conn.commit()
            child_id = lodge_lead(
                conn,
                "capex",
                "AMZN",
                "NASDAQ",
                "capex",
                "2026-03-01-capex",
                parent_position_id=position_id,
                summary="later filing near the open position",
            )
            child = conn.execute(
                "SELECT status, parent_position_id, outcome FROM leads WHERE lead_id = ?",
                (child_id,),
            ).fetchone()
            self.assertEqual(child["status"], "fresh")
            self.assertEqual(child["parent_position_id"], position_id)
            self.assertIsNone(child["outcome"])
            self.assertEqual(
                conn.execute(
                    "SELECT figure_id FROM lead_figures WHERE lead_id = ?",
                    (position_id,),
                ).fetchone()["figure_id"],
                figure_id,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM lead_figures WHERE lead_id = ?",
                    (position_id,),
                ).fetchone()[0],
                1,
            )
            acted_id = lodge_lead(
                conn,
                "debt_raise",
                "SCG",
                "ASX",
                "debt_raise",
                "2026-03-01-capex",
            )
            conn.execute(
                "UPDATE leads SET status = 'acted', updated_at = '2026-03-04T00:00:00Z' WHERE lead_id = ?",
                (acted_id,),
            )
            conn.commit()
            acted_child = lodge_lead(
                conn,
                "debt_raise",
                "SCG",
                "ASX",
                "debt_raise",
                "2026-03-01-capex",
                parent_position_id=acted_id,
            )
            self.assertEqual(
                conn.execute(
                    "SELECT parent_position_id FROM leads WHERE lead_id = ?",
                    (acted_child,),
                ).fetchone()["parent_position_id"],
                acted_id,
            )
            fresh_id = lodge_lead(
                conn,
                "capex",
                "MSFT",
                "NASDAQ",
                "capex",
                "2026-03-01-capex",
            )
            with self.assertRaises(sqlite3.IntegrityError):
                lodge_lead(
                    conn,
                    "capex",
                    "MSFT",
                    "NASDAQ",
                    "capex",
                    "2026-03-01-capex",
                    parent_position_id=fresh_id,
                )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE leads SET parent_position_id = lead_id WHERE lead_id = ?",
                    (position_id,),
                )
            self.assertIsNone(
                conn.execute(
                    "SELECT parent_position_id FROM leads WHERE lead_id = ?",
                    (position_id,),
                ).fetchone()["parent_position_id"]
            )
            with self.assertRaises(ValueError):
                lodge_lead(conn, "other", "AMZN", "NASDAQ", "capex", "2026-03-01-capex")

            criteria = list(
                conn.execute(
                    """
                    SELECT c.criterion_id
                    FROM rubric_criteria c
                    JOIN rubrics r ON r.rubric_id = c.rubric_id
                    WHERE r.lead_type = 'capex' AND r.version = 1
                    ORDER BY c.sort_order
                    """
                )
            )
            self.assertGreaterEqual(len(criteria), 3)
            conn.execute(
                """
                INSERT INTO theses (
                    lead_id, run_id, thesis_text, risks, quality_score, written_at
                ) VALUES (?, '2026-03-02-centurion', 'Demand is not priced in.', 'The spend can slip.', 0, '2026-03-02T00:00:00Z')
                """,
                (position_id,),
            )
            conn.commit()
            record_criterion_result(
                conn,
                position_id,
                criteria[0]["criterion_id"],
                True,
                "2026-03-02-centurion",
                evidence="above the default",
                source_url="https://www.sec.gov/Archives/edgar/data/1018724/",
            )
            record_criterion_result(
                conn,
                position_id,
                criteria[1]["criterion_id"],
                0,
                "2026-03-02-centurion",
                evidence="below the default",
            )
            score = record_criterion_result(
                conn,
                position_id,
                criteria[2]["criterion_id"],
                1,
                "2026-03-02-centurion",
                evidence="filing",
            )
            self.assertEqual(score, 2)
            self.assertEqual(compute_quality_score(conn, position_id), 2)
            stored = conn.execute(
                "SELECT quality_score, outcome, status FROM leads WHERE lead_id = ?",
                (position_id,),
            ).fetchone()
            self.assertEqual(stored["quality_score"], 2)
            self.assertIsNone(stored["outcome"])
            self.assertEqual(stored["status"], "position")
            thesis = conn.execute(
                "SELECT quality_score, risks, thesis_text FROM theses WHERE lead_id = ?",
                (position_id,),
            ).fetchone()
            self.assertEqual(thesis["quality_score"], 2)
            self.assertEqual(thesis["risks"], "The spend can slip.")
            self.assertEqual(thesis["thesis_text"], "Demand is not priced in.")
            score = record_criterion_result(
                conn,
                position_id,
                criteria[1]["criterion_id"],
                True,
                "2026-03-02-centurion",
                evidence="revised above the default",
            )
            self.assertEqual(score, 3)
            self.assertEqual(
                conn.execute(
                    "SELECT quality_score FROM theses WHERE lead_id = ?",
                    (position_id,),
                ).fetchone()["quality_score"],
                3,
            )
            conn.execute(
                "UPDATE leads SET outcome = 'buy' WHERE lead_id = ?",
                (position_id,),
            )
            conn.commit()
            after = conn.execute(
                "SELECT status, quality_score, outcome FROM leads WHERE lead_id = ?",
                (position_id,),
            ).fetchone()
            self.assertEqual(after["status"], "position")
            self.assertEqual(after["quality_score"], 3)
            self.assertEqual(after["outcome"], "buy")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE leads SET outcome = '   ' WHERE lead_id = ?",
                    (position_id,),
                )
            conn.execute(
                "UPDATE leads SET status = 'closed' WHERE lead_id = ?",
                (child_id,),
            )
            conn.commit()
            self.assertEqual(
                conn.execute(
                    "SELECT status FROM leads WHERE lead_id = ?",
                    (child_id,),
                ).fetchone()["status"],
                "closed",
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE leads SET status = 'nope' WHERE lead_id = ?",
                    (child_id,),
                )

            thesis_id = conn.execute(
                "SELECT thesis_id FROM theses WHERE lead_id = ?",
                (position_id,),
            ).fetchone()["thesis_id"]
            conn.execute(
                """
                INSERT INTO critiques (
                    thesis_id, run_id, critique, verdict, round, written_at
                ) VALUES (?, '2026-03-03-praetorian', 'The growth comparison is one year, not a trend.', 'revise', 1, '2026-03-03T00:00:00Z')
                """,
                (thesis_id,),
            )
            merge = conn.execute(
                """
                INSERT INTO legate_merges (
                    created_at, survivor_lead_id, rationale, priority_rank, bias_audit, audited, run_id
                ) VALUES (
                    '2026-03-03T00:00:00Z', ?, 'same spend, two filings', 1,
                    'no lead type always survives', 1, '2026-03-03-praetorian'
                )
                """,
                (position_id,),
            )
            conn.execute(
                "INSERT INTO legate_merge_leads (merge_id, lead_id) VALUES (?, ?)",
                (merge.lastrowid, child_id),
            )
            conn.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO legate_merge_leads (merge_id, lead_id) VALUES (?, ?)",
                    (merge.lastrowid, position_id),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO legate_merges (
                        created_at, survivor_lead_id, bias_audit, audited
                    ) VALUES ('2026-03-03T00:00:00Z', ?, 'unchecked', 0)
                    """,
                    (position_id,),
                )
            indexes = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                )
            }
            self.assertIn("ix_leads_parent", indexes)
            self.assertIn("ix_results_run", indexes)
            self.assertIn("ix_merges_survivor", indexes)
            conn.close()

    def _assert_threshold(self, conn, lead_type, name, value, unit):
        row = conn.execute(
            """
            SELECT c.threshold_value, c.threshold_unit, c.configurable
            FROM rubric_criteria c
            JOIN rubrics r ON r.rubric_id = c.rubric_id
            WHERE r.lead_type = ? AND r.version = 1 AND c.name = ?
            """,
            (lead_type, name),
        ).fetchone()
        self.assertIsNotNone(row, name)
        self.assertEqual(row["threshold_value"], value)
        self.assertEqual(row["threshold_unit"], unit)
        self.assertEqual(row["configurable"], 1)


if __name__ == "__main__":
    unittest.main()
