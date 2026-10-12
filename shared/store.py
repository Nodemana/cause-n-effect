"""SQLite history shared by the scouts. The figures table is defined in schema.sql."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_NAME = "shared_schema.sql"
HERE = Path(__file__).resolve().parent
SCHEMA = HERE / "schema.sql"

_FIGURE_COLUMNS = (
    "run_id",
    "ticker",
    "company",
    "figure_type",
    "instrument",
    "period",
    "value",
    "value_text",
    "currency",
    "seen_at",
    "announced_on",
    "source_url",
    "source_type",
    "verified",
)


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    ensure_history(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def ensure_history(conn):
    """Create runs, figures, leads, and the changes view. Idempotent."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            name TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )
    seen = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE name = ?", (SCHEMA_NAME,)
    ).fetchone()
    if not seen:
        conn.executescript(SCHEMA.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)",
            (SCHEMA_NAME, utc_now()),
        )
    conn.commit()


def save_run(conn, run_id, scout, started_at, figures, notes=None):
    """Replace one run's figures. Leads are left for the Legate."""
    ensure_history(conn)
    with conn:
        conn.execute(
            """
            INSERT INTO runs (run_id, scout, started_at, notes)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                scout = excluded.scout,
                started_at = excluded.started_at,
                notes = excluded.notes
            """,
            (run_id, scout, started_at, notes),
        )
        conn.execute("DELETE FROM figures WHERE run_id = ?", (run_id,))
        conn.executemany(
            f"INSERT INTO figures ({', '.join(_FIGURE_COLUMNS)}) VALUES ({', '.join('?' * len(_FIGURE_COLUMNS))})",
            [_figure_row(run_id, row) for row in figures],
        )
    return run_id


def _figure_row(run_id, row):
    return (
        run_id,
        row["ticker"],
        row.get("company"),
        row["figure_type"],
        row.get("instrument") or "",
        row.get("period") or "",
        row.get("value"),
        row.get("value_text"),
        row.get("currency"),
        row["seen_at"],
        row.get("announced_on"),
        row.get("source_url"),
        row.get("source_type"),
        int(row.get("verified", 0)),
    )
