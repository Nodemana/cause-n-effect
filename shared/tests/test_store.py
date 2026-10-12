"""Shared history. No network."""

import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from shared.store import allocate_run_id, connect, make_run_id, save_run  # noqa: E402


SCOUTS = (
    "debt-raise",
    "insider-buying",
    "capex",
    "orders-backlog",
    "accumulation",
)


class StoreTests(unittest.TestCase):
    def test_run_id_is_date_utc_time_and_scout(self):
        started = "2026-10-12T14:30:52Z"
        for scout in SCOUTS:
            self.assertEqual(make_run_id(started, scout), f"2026-10-12-143052-{scout}")
        with self.assertRaises(ValueError):
            make_run_id("2026-10-12", "capex")

    def test_same_day_runs_keep_their_figures(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "history.sqlite")
            try:
                first = save_run(conn, "capex", "2026-10-12T08:15:00Z", [_figure(25)])
                second = save_run(conn, "capex", "2026-10-12T18:45:09Z", [_figure(11)])
                self.assertEqual(first, "2026-10-12-081500-capex")
                self.assertEqual(second, "2026-10-12-184509-capex")
                stored = {
                    row["run_id"]: row["value"]
                    for row in conn.execute("SELECT run_id, value FROM figures")
                }
                self.assertEqual(stored[first], 25)
                self.assertEqual(stored[second], 11)
            finally:
                conn.close()

    def test_same_second_run_does_not_replace_figures(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "history.sqlite")
            try:
                first = save_run(conn, "capex", "2026-10-12T08:15:00Z", [_figure(25)])
                second = save_run(conn, "capex", "2026-10-12T08:15:00Z", [_figure(11)])
                self.assertEqual(first, "2026-10-12-081500-capex")
                self.assertEqual(second, "2026-10-12-081500-capex-2")
                stored = {
                    row["run_id"]: row["value"]
                    for row in conn.execute("SELECT run_id, value FROM figures")
                }
                self.assertEqual(stored, {first: 25, second: 11})
            finally:
                conn.close()

    def test_legacy_daily_run_stays_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "history.sqlite")
            try:
                conn.execute(
                    "INSERT INTO runs (run_id, scout, started_at, notes) VALUES (?, ?, ?, ?)",
                    ("2026-10-12-capex", "capex", "2026-10-12T01:00:00Z", "already stored"),
                )
                conn.execute(
                    """
                    INSERT INTO figures (
                        run_id, ticker, figure_type, instrument, period,
                        value, value_text, seen_at, verified
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "2026-10-12-capex",
                        "AMZN",
                        "PaymentsToAcquireProductiveAssets",
                        "",
                        "2025-01-01/2025-03-31",
                        1,
                        "1",
                        "2026-10-12",
                        1,
                    ),
                )
                conn.commit()
                fresh = save_run(conn, "capex", "2026-10-12T02:03:04Z", [_figure(9)])
                self.assertEqual(fresh, "2026-10-12-020304-capex")
                legacy = conn.execute(
                    "SELECT value FROM figures WHERE run_id = ?",
                    ("2026-10-12-capex",),
                ).fetchone()
                self.assertEqual(legacy["value"], 1)
                again = conn.execute(
                    "SELECT value_text, verified FROM changes WHERE run_id = ?",
                    ("2026-10-12-capex",),
                ).fetchone()
                self.assertEqual(again["value_text"], "1")
                self.assertEqual(again["verified"], 1)
                self.assertEqual(
                    conn.execute("SELECT value FROM figures WHERE run_id = ?", (fresh,)).fetchone()["value"],
                    9,
                )
            finally:
                conn.close()

    def test_allocate_leaves_an_existing_id_unused(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "history.sqlite")
            try:
                for scout in SCOUTS:
                    run_id = allocate_run_id(conn, "2026-10-12T14:30:52Z", scout)
                    conn.execute(
                        "INSERT INTO runs (run_id, scout, started_at) VALUES (?, ?, ?)",
                        (run_id, scout, "2026-10-12T14:30:52Z"),
                    )
                    conn.commit()
                    nxt = allocate_run_id(conn, "2026-10-12T14:30:52Z", scout)
                    self.assertEqual(run_id, f"2026-10-12-143052-{scout}")
                    self.assertEqual(nxt, f"{run_id}-2")
                    self.assertEqual(
                        conn.execute("SELECT COUNT(*) FROM figures WHERE run_id = ?", (run_id,)).fetchone()[0],
                        0,
                    )
            finally:
                conn.close()


def _figure(value):
    return {
        "ticker": "AMZN",
        "figure_type": "PaymentsToAcquireProductiveAssets",
        "period": "2025-01-01/2025-03-31",
        "value": value,
        "value_text": str(value),
        "seen_at": "2026-10-12",
        "verified": 1,
    }


if __name__ == "__main__":
    unittest.main()
