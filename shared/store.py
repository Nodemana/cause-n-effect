"""SQLite history shared by the scouts. The figures table is defined in schema.sql."""

import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_QUARTER_FRAME = re.compile(r"CY\d{4}Q[1-4]\Z")

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


def figure_key(row):
    """The figures unique key, aside from run_id."""
    return (
        row.get("ticker") or "",
        row.get("figure_type") or "",
        row.get("instrument") or "",
        row.get("period") or "",
    )


def prefer_fact(candidate, incumbent):
    """True when `candidate` should replace `incumbent`.

    A single-quarter value outranks a year-to-date or annual value. When that
    specificity ties, the most recent filing wins.
    """
    return _rank(candidate) > _rank(incumbent)


def dedupe_figures(figures):
    """One row per ticker, type, instrument, and period. Dropped rows are logged."""
    kept = {}
    order = []
    for row in figures or []:
        key = figure_key(row)
        current = kept.get(key)
        if current is None:
            kept[key] = row
            order.append(key)
            continue
        if prefer_fact(row, current):
            log_duplicate(current, row)
            kept[key] = row
        else:
            log_duplicate(row, current)
    return [kept[key] for key in order]


def log_duplicate(dropped, kept):
    """Say which figure lost the unique key, and which one was kept."""
    log.warning(
        "dropped duplicate figure ticker=%s type=%s instrument=%s period=%s "
        "value=%s currency=%s announced_on=%s; kept value=%s currency=%s announced_on=%s",
        dropped.get("ticker") or "",
        dropped.get("figure_type") or "",
        dropped.get("instrument") or "",
        dropped.get("period") or "",
        dropped.get("value"),
        dropped.get("currency") or "",
        _filed(dropped),
        kept.get("value"),
        kept.get("currency") or "",
        _filed(kept),
    )


def save_run(conn, run_id, scout, started_at, figures, notes=None):
    """Replace one run's figures. Leads are left for the Legate.

    Rows that still share a figures key are skipped, so one duplicate cannot
    roll the run back.
    """
    ensure_history(conn)
    figures = dedupe_figures(figures)
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
        _insert_figures(conn, run_id, figures)
    return run_id


def _insert_figures(conn, run_id, figures):
    columns = ", ".join(_FIGURE_COLUMNS)
    placeholders = ", ".join("?" * len(_FIGURE_COLUMNS))
    conn.executemany(
        f"""
        INSERT INTO figures ({columns}) VALUES ({placeholders})
        ON CONFLICT(run_id, ticker, figure_type, instrument, period) DO NOTHING
        """,
        [_figure_row(run_id, row) for row in figures],
    )


def _filed(row):
    raw = row.get("announced_on") or row.get("filed") or ""
    return str(raw)[:10]


def _rank(row):
    return (_specificity(row), _filed(row))


def _specificity(row):
    """3 is a single quarter, 2 is unknown, 1 is year-to-date, 0 is a full year."""
    frame = str(row.get("frame") or "")
    if _QUARTER_FRAME.fullmatch(frame):
        return 3
    if frame.endswith("YTD"):
        return 1
    if re.fullmatch(r"CY\d{4}", frame):
        return 0
    fp = str(row.get("fp") or "").upper()
    days = _span_days(row)
    if fp in {"Q1", "Q2", "Q3", "Q4"} and days is not None and days <= 100:
        return 3
    if fp == "FY" or (days is not None and days > 300):
        return 0
    if days is not None and days <= 100:
        return 3
    if days is not None:
        return 1
    return 2


def _span_days(row):
    start = row.get("start")
    end = row.get("end")
    if not start or not end:
        period = row.get("period") or ""
        if isinstance(period, str) and "/" in period:
            start, _, end = period.partition("/")
    if not isinstance(start, str) or not isinstance(end, str):
        return None
    try:
        began = datetime.strptime(start, "%Y-%m-%d")
        ended = datetime.strptime(end, "%Y-%m-%d")
    except ValueError:
        return None
    days = (ended - began).days
    if days < 0:
        return None
    return days


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
