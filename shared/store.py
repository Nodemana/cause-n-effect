"""SQLite history shared by the scouts. The figures table is defined in schema.sql."""

import re
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


_STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})Z")


def make_run_id(started_at, scout):
    """UTC date, UTC HHMMSS, and scout. Example: 2026-10-12-143052-capex."""
    match = _STAMP.match(started_at or "")
    if not match:
        raise ValueError("started_at must be UTC YYYY-MM-DDTHH:MM:SSZ")
    day, hour, minute, second = match.groups()
    slug = (scout or "").strip()
    if not slug or any(char.isspace() for char in slug):
        raise ValueError("scout name is required")
    return f"{day}-{hour}{minute}{second}-{slug}"


def allocate_run_id(conn, started_at, scout):
    """A run id that is not already stored. An existing row is left as it is."""
    base = make_run_id(started_at, scout)
    candidate = base
    suffix = 2
    while conn.execute("SELECT 1 FROM runs WHERE run_id = ?", (candidate,)).fetchone():
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


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


def save_run(conn, scout, started_at, figures, notes=None):
    """Insert one new run and its figures. Return the run id.

    The id is the UTC date, the UTC time as HHMMSS, and the scout name.
    A second run that starts in the same second gets a numeric suffix.
    Figures already stored for another run are not updated or deleted.
    Leads are left for the Legate.
    """
    ensure_history(conn)
    rows = _unique_figures(figures)
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        run_id = allocate_run_id(conn, started_at, scout)
        conn.execute(
            """
            INSERT INTO runs (run_id, scout, started_at, notes)
            VALUES (?, ?, ?, ?)
            """,
            (run_id, scout, started_at, notes),
        )
        conn.executemany(
            f"INSERT INTO figures ({', '.join(_FIGURE_COLUMNS)}) VALUES ({', '.join('?' * len(_FIGURE_COLUMNS))})",
            [_figure_row(run_id, row) for row in rows],
        )
    return run_id


def _unique_figures(figures):
    """Keep every figure. A repeated key gets a period suffix instead of a replace."""
    used = set()
    kept = []
    for item in figures:
        row = dict(item)
        period = row.get("period") or ""
        instrument = row.get("instrument") or ""
        key = (row["ticker"], row["figure_type"], instrument, period)
        copy = 2
        while key in used:
            period = f"{row.get('period') or ''}|{copy}"
            key = (row["ticker"], row["figure_type"], instrument, period)
            copy += 1
        row["period"] = period
        row["instrument"] = instrument
        used.add(key)
        kept.append(row)
    return kept


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
