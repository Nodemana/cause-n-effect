"""SQLite history: shared runs/figures/leads plus debt-raise analytics."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
MIGRATIONS = HERE / "migrations"


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def open_db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def migrate(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            name TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )
    from shared.store import ensure_history

    ensure_history(conn)
    for path in sorted(MIGRATIONS.glob("*.sql")):
        seen = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE name = ?", (path.name,)
        ).fetchone()
        if seen:
            continue
        conn.executescript(path.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)",
            (path.name, utc_now()),
        )
    conn.commit()


def allocate_run_id(conn, started_at):
    from shared.store import allocate_run_id as allocate

    return allocate(conn, started_at, "debt-raise")


def save_run(
    conn,
    started_at,
    threshold,
    seen,
    cheap_n,
    full_n,
    score_histogram,
    scores,
    figures,
    notes,
    scope=None,
):
    from shared.store import ensure_history, format_scope

    ensure_history(conn)
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        run_id = allocate_run_id(conn, started_at)
        conn.execute(
            "INSERT INTO runs (run_id, scout, started_at, notes, scope) VALUES (?, ?, ?, ?, ?)",
            (run_id, "debt-raise", started_at, notes, format_scope(scope)),
        )
        conn.execute(
            """
            INSERT INTO debt_scout_runs (
                run_id, started_at, threshold, filings_seen,
                resolved_by_cheap_filter, sent_to_full_document, score_histogram
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                started_at,
                threshold,
                seen,
                cheap_n,
                full_n,
                json.dumps(score_histogram),
            ),
        )
        conn.executemany(
            """
            INSERT INTO debt_scout_filing_scores (
                run_id, accession, ticker, score, stage_reached, form,
                cheap_score, decided, flags
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    run_id,
                    row["accession"],
                    row["ticker"],
                    row["score"],
                    row["stage_reached"],
                    row["form"],
                    row["cheap_score"],
                    row["decided"],
                    row["flags"],
                )
                for row in scores
            ],
        )
        conn.executemany(
            """
            INSERT INTO figures (
                run_id, ticker, company, figure_type, instrument, period,
                value, value_text, currency, seen_at, announced_on,
                source_url, source_type, verified
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    run_id,
                    row["ticker"],
                    row.get("company"),
                    row["figure_type"],
                    row.get("instrument") or "",
                    row.get("period") or "",
                    row["value"],
                    row["value_text"],
                    row["currency"],
                    row["seen_at"],
                    row.get("announced_on"),
                    row.get("source_url"),
                    row.get("source_type"),
                    int(row.get("verified", 1)),
                )
                for row in figures
            ],
        )
    return run_id
