"""SQLite cache for macro series. Migrations follow the debt-raise journal."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from shared.store import ensure_history, utc_now

HERE = Path(__file__).resolve().parent
MIGRATIONS = HERE / "migrations"

_SERIES_COLUMNS = (
    "series_id",
    "name",
    "source",
    "source_url",
    "unit",
    "frequency",
    "stale_after_hours",
)


def open_db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def migrate(conn):
    """Apply shared history, then each macro migration once."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            name TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )
    ensure_history(conn)
    for path in sorted(MIGRATIONS.glob("*.sql")):
        seen = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE name = ?",
            (path.name,),
        ).fetchone()
        if seen:
            continue
        conn.executescript(path.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)",
            (path.name, utc_now()),
        )
    conn.commit()


def upsert_series(conn, spec):
    columns = ", ".join(_SERIES_COLUMNS)
    placeholders = ", ".join("?" * len(_SERIES_COLUMNS))
    updates = ", ".join(f"{name} = excluded.{name}" for name in _SERIES_COLUMNS if name != "series_id")
    conn.execute(
        f"""
        INSERT INTO macro_series ({columns}) VALUES ({placeholders})
        ON CONFLICT(series_id) DO UPDATE SET {updates}
        """,
        tuple(spec[name] for name in _SERIES_COLUMNS),
    )


def save_observations(conn, series_id, observations, fetched_at):
    conn.executemany(
        """
        INSERT INTO macro_observations (series_id, obs_date, value, fetched_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(series_id, obs_date) DO UPDATE SET
            value = excluded.value,
            fetched_at = excluded.fetched_at
        """,
        [
            (series_id, row["obs_date"], row["value"], fetched_at)
            for row in observations
        ],
    )
    return len(observations)


def latest_fetched_at(conn, series_id):
    row = conn.execute(
        """
        SELECT MAX(fetched_at) AS fetched_at
        FROM macro_observations
        WHERE series_id = ?
        """,
        (series_id,),
    ).fetchone()
    if row is None:
        return None
    return row["fetched_at"]


def is_stale(conn, series_id, stale_after_hours, now):
    """True when the newest fetched_at is missing or at least stale_after_hours old."""
    fetched = latest_fetched_at(conn, series_id)
    if not fetched:
        return True
    try:
        fetched_at = _stamp(fetched)
        moment = _stamp(now)
    except ValueError:
        return True
    age_hours = (moment - fetched_at).total_seconds() / 3600
    return age_hours >= stale_after_hours


def _stamp(value):
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
