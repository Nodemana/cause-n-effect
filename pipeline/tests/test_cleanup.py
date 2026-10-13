"""Database cleanup. Dry-run writes nothing. Apply is idempotent."""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from pipeline import cleanup as cleanup_module  # noqa: E402
from pipeline.cleanup import cleanup_database  # noqa: E402
from pipeline.cli import main  # noqa: E402
from shared.pipeline_store import connect, lodge_lead  # noqa: E402
from shared.store import save_run  # noqa: E402


class CleanupTests(unittest.TestCase):
    def test_each_rule_including_gev_and_etn(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "history.sqlite")
            conn = connect(path)
            try:
                _seed(conn)
                gev = conn.execute(
                    "SELECT figure_id, run_id FROM figures WHERE ticker = 'GEV' AND value = 11100000000"
                ).fetchone()
                lead_id = lodge_lead(
                    conn,
                    "orders_backlog",
                    "GEV",
                    "US",
                    "orders_backlog",
                    gev["run_id"],
                    figure_ids=[gev["figure_id"]],
                    company="GE Vernova",
                )
                conn.execute(
                    """
                    INSERT INTO theses (lead_id, run_id, thesis_text, risks, quality_score, written_at)
                    VALUES (?, ?, 'GEV backlog', '', 0, '2026-10-12T00:00:00Z')
                    """,
                    (lead_id, gev["run_id"]),
                )
                dangling_run = conn.execute(
                    "SELECT run_id FROM figures WHERE ticker = 'GEV' AND value = 176000000000"
                ).fetchone()["run_id"]
                dangling_lead = lodge_lead(
                    conn,
                    "orders_backlog",
                    "GEV",
                    "US",
                    "orders_backlog",
                    dangling_run,
                    company="GE Vernova",
                )
                conn.execute(
                    """
                    INSERT INTO theses (lead_id, run_id, thesis_text, risks, quality_score, written_at)
                    VALUES (?, ?, 'dangling', '', 0, '2026-10-12T00:00:00Z')
                    """,
                    (dangling_lead, dangling_run),
                )
                conn.commit()
                conn.execute("PRAGMA foreign_keys = OFF")
                conn.execute("DELETE FROM leads WHERE lead_id = ?", (dangling_lead,))
                conn.commit()
                conn.execute("PRAGMA foreign_keys = ON")
            finally:
                conn.close()

            preview = cleanup_database(path, apply=False, now="2026-10-12T18:00:00Z")
            self.assertFalse(preview["apply"])
            self.assertIsNone(preview["backup"])
            self.assertGreaterEqual(preview["summary"]["implausible_figure"], 1)
            self.assertGreaterEqual(preview["summary"]["duplicate_figure"], 1)
            self.assertGreaterEqual(preview["summary"]["superseded_figure"], 1)
            self.assertGreaterEqual(preview["summary"]["prior_year_comparative"], 1)
            self.assertGreaterEqual(preview["summary"]["unofficial_source"], 1)
            self.assertGreaterEqual(preview["summary"]["orphan_lead"], 1)
            self.assertGreaterEqual(preview["summary"]["orphan_thesis"], 1)
            conn = connect(path)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM cleanup_log").fetchone()[0], 0)
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM figures WHERE value = 11100000000").fetchone()[0],
                    1,
                )
            finally:
                conn.close()
            self.assertEqual(list(Path(tmp).glob("*.backup-*")), [])

            applied = cleanup_database(path, apply=True, now="2026-10-12T18:00:00Z")
            self.assertIsNotNone(applied["backup"])
            backup = sqlite3.connect(applied["backup"])
            try:
                self.assertEqual(
                    backup.execute("SELECT COUNT(*) FROM figures WHERE value = 11100000000").fetchone()[0],
                    1,
                )
            finally:
                backup.close()
            conn = connect(path)
            try:
                values = {row["value"] for row in conn.execute("SELECT value FROM figures")}
                reasons = {
                    row["reason"]
                    for row in conn.execute("SELECT reason FROM cleanup_log")
                }
                kept_duplicate = conn.execute(
                    """
                    SELECT r.started_at FROM figures AS f
                    JOIN runs AS r ON r.run_id = f.run_id
                    WHERE f.ticker = 'ETN' AND f.instrument = 'Amazon'
                    """
                ).fetchone()
                log_count = conn.execute("SELECT COUNT(*) FROM cleanup_log").fetchone()[0]
            finally:
                conn.close()
            self.assertIn(176_000_000_000, values)
            self.assertNotIn(11_100_000_000, values)
            self.assertNotIn(15_175_000_000, values)
            self.assertIn(15_200_000_000, values)
            self.assertIn(5_000_000_000, values)
            self.assertIn(1_000_000, values)
            self.assertNotIn(1_000_000.5, values)
            self.assertNotIn(99_000_000, values)
            self.assertEqual(kept_duplicate["started_at"], "2026-05-01T00:00:00Z")
            self.assertIn("implausible_figure", reasons)
            self.assertIn("duplicate_figure", reasons)
            self.assertIn("superseded_figure", reasons)
            self.assertIn("prior_year_comparative", reasons)
            self.assertIn("unofficial_source", reasons)
            self.assertIn("orphan_lead", reasons)
            self.assertIn("orphan_thesis", reasons)
            self.assertNotIn(20_000_000_000, values)

            again = cleanup_database(path, apply=True, now="2026-10-12T19:00:00Z")
            self.assertEqual(again["removed"], 0)
            self.assertIsNone(again["backup"])
            self.assertTrue(all(count == 0 for count in again["summary"].values()))
            conn = connect(path)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM cleanup_log").fetchone()[0], log_count)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM theses").fetchone()[0], 0)
                amzn = {
                    row["period"]
                    for row in conn.execute("SELECT period FROM figures WHERE ticker = 'AMZN'")
                }
            finally:
                conn.close()
            self.assertEqual(
                amzn,
                {"2026-07-01/2026-09-30", "2025-07-01/2025-09-30"},
            )

    def test_a_failed_delete_rolls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "history.sqlite")
            conn = connect(path)
            try:
                save_run(
                    conn,
                    "orders-backlog",
                    "2026-05-01T00:00:00Z",
                    [
                        _order("ETN", "Amazon", "2026-06-30", 5_000_000),
                        _order("ETN", "near", "2026-01-01", 1_000_000),
                    ],
                )
                save_run(
                    conn,
                    "orders-backlog",
                    "2026-08-02T00:00:00Z",
                    [
                        _order("ETN", "Amazon", "2026-06-30", 5_000_000),
                        _order("ETN", "near", "2026-01-01", 1_000_000),
                    ],
                )
            finally:
                conn.close()
            original = cleanup_module._delete_row
            calls = {"n": 0}

            def boom(db, item):
                calls["n"] += 1
                if calls["n"] == 2:
                    raise RuntimeError("boom")
                original(db, item)

            cleanup_module._delete_row = boom
            try:
                with self.assertRaises(RuntimeError):
                    cleanup_database(path, apply=True, now="2026-10-12T18:00:00Z")
            finally:
                cleanup_module._delete_row = original
            conn = connect(path)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM figures").fetchone()[0], 4)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM cleanup_log").fetchone()[0], 0)
            finally:
                conn.close()

    def test_cli_defaults_to_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "history.sqlite")
            conn = connect(path)
            try:
                save_run(
                    conn,
                    "orders-backlog",
                    "2026-07-22T00:00:00Z",
                    [
                        _order("GEV", "backlog", "2026-06-30", 176_000_000_000),
                        _order("GEV", "backlog", "2026-03-31", 11_100_000_000),
                    ],
                )
            finally:
                conn.close()
            import io
            from contextlib import redirect_stdout

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = main(["cleanup", "--db", path])
            self.assertEqual(code, 0)
            self.assertIn("implausible_figure: 1", stdout.getvalue())
            self.assertIn("apply: false", stdout.getvalue())
            self.assertIn("backup: -", stdout.getvalue())
            conn = connect(path)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM figures").fetchone()[0], 2)
            finally:
                conn.close()
            with self.assertRaises(SystemExit):
                main(["cleanup", "--db", path, "--dry-run", "--apply"])


def _seed(conn):
    save_run(
        conn,
        "orders-backlog",
        "2026-07-22T00:00:00Z",
        [
            _order("GEV", "backlog", "2026-06-30", 176_000_000_000, announced_on="2026-07-22"),
            _order("GEV", "backlog", "2026-03-31", 11_100_000_000, announced_on="2026-07-22"),
            _order("AMZN", "quarter", "2026-07-01/2026-09-30", 5_000_000_000, figure_type="PaymentsToAcquireProductiveAssets"),
            _order("AMZN", "quarter", "2025-07-01/2025-09-30", 1_000_000_000, figure_type="PaymentsToAcquireProductiveAssets"),
        ],
    )
    save_run(
        conn,
        "orders-backlog",
        "2026-05-01T00:00:00Z",
        [
            _order("ETN", "Amazon", "2026-06-30", 5_000_000, announced_on="2026-05-01"),
            _order("ETN", "near", "2026-01-01", 1_000_000, announced_on="2026-05-01"),
            _order(
                "ETN",
                "backlog:Electrical Americas",
                "2026-06-30",
                15_175_000_000,
                announced_on="2026-05-01",
            ),
        ],
    )
    save_run(
        conn,
        "orders-backlog",
        "2026-08-02T00:00:00Z",
        [
            _order("ETN", "Amazon", "2026-06-30", 5_000_000, announced_on="2026-08-01"),
            _order("ETN", "near", "2026-01-01", 1_000_000.5, announced_on="2026-08-01"),
            _order(
                "ETN",
                "backlog:Electrical Americas",
                "2026-06-30",
                15_200_000_000,
                announced_on="2026-08-01",
            ),
            _order(
                "ETN",
                "backlog",
                "2025-12-31",
                20_000_000_000,
                announced_on="2026-08-05",
                source_type="10-Q",
            ),
            _order(
                "ETN",
                "quote",
                "2026-06-30",
                99_000_000,
                source_url="https://example.com/quote",
                source_type="media",
            ),
        ],
    )


def _order(
    ticker,
    instrument,
    period,
    value,
    figure_type="backlog",
    announced_on="2026-07-22",
    source_url=None,
    source_type="filing",
):
    return {
        "ticker": ticker,
        "company": ticker,
        "figure_type": figure_type,
        "instrument": instrument,
        "period": period,
        "value": value,
        "value_text": str(value),
        "currency": "USD",
        "seen_at": "2026-10-12",
        "announced_on": announced_on,
        "source_url": source_url or f"https://www.sec.gov/Archives/edgar/data/1/{ticker.lower()}.htm",
        "source_type": source_type,
        "verified": 1,
    }


if __name__ == "__main__":
    unittest.main()
