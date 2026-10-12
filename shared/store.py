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
    _ensure_run_scope(conn)
    conn.commit()


def _ensure_run_scope(conn):
    """Add runs.scope on a history file created before that column existed."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
    if columns and "scope" not in columns:
        conn.execute("ALTER TABLE runs ADD COLUMN scope TEXT")


def format_scope(scope):
    """Comma-separated tickers, or None for a full-universe run.

    `None`, `""`, and `"all"` are a full universe. A list or a comma string
    is only those tickers. Matching is case-insensitive and keeps a `.AX` suffix.
    """
    if scope is None:
        return None
    if isinstance(scope, str):
        if scope.strip().casefold() == "all":
            return None
        parts = [part.strip() for part in scope.split(",")]
    else:
        parts = [str(part).strip() for part in scope]
    seen = []
    for part in parts:
        if not part:
            continue
        token = part.upper()
        if token not in seen:
            seen.append(token)
    if not seen:
        return None
    return ",".join(seen)


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


def save_run(conn, scout, started_at, figures, notes=None, scope=None):
    """Insert one new run and its figures. Return the run id.

    The id is the UTC date, the UTC time as HHMMSS, and the scout name.
    A second run that starts in the same second gets a numeric suffix.
    Figures already stored for another run are not updated or deleted.
    Duplicate keys in this batch are collapsed first (single-quarter, then
    latest filing; drops are logged). A leftover duplicate is skipped.
    Leads are left for the Legate.

    `scope` is the ticker list for a partial run (`--tickers`). None stores
    a full-universe run, which supersedes every ticker when the pipeline loads.
    """
    ensure_history(conn)
    rows = _unique_figures(dedupe_figures(figures))
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        run_id = allocate_run_id(conn, started_at, scout)
        conn.execute(
            """
            INSERT INTO runs (run_id, scout, started_at, notes, scope)
            VALUES (?, ?, ?, ?, ?)
            """,
            (run_id, scout, started_at, notes, format_scope(scope)),
        )
        _insert_figures(conn, run_id, rows)
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
