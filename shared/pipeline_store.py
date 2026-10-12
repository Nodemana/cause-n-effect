"""Pipeline tables on top of the shared scout history.

ensure_pipeline applies shared/migrations/001_pipeline.sql, then
002_lead_pipeline.sql, then 003_holder_role.sql, then
004_lead_counterparties.sql, then 005_deal_party_role.sql, then
006_agent_handoff.sql, then 008_macro_leads.sql, then
009_praetorian_flaws.sql, then 010_legate_selection.sql, and records each
filename in schema_migrations.
An older leads table (no lead_id) is rebuilt and its rows are kept.
lodge_lead, record_criterion_result, and compute_quality_score are the
write path. quality_score is the number of criteria with passed = 1.
"""

import sqlite3
from pathlib import Path

from shared.store import ensure_history, utc_now

HERE = Path(__file__).resolve().parent
MIGRATION_NAME = "001_pipeline.sql"
MIGRATION_PATH = HERE / "migrations" / MIGRATION_NAME
MIGRATIONS = (
    "001_pipeline.sql",
    "002_lead_pipeline.sql",
    "003_holder_role.sql",
    "004_lead_counterparties.sql",
    "005_deal_party_role.sql",
    "006_agent_handoff.sql",
    "008_macro_leads.sql",
    "009_praetorian_flaws.sql",
    "010_legate_selection.sql",
)

LEAD_TYPES = (
    "capex",
    "debt_raise",
    "insider_buying",
    "orders_backlog",
    "accumulation",
    "macro",
)
STATUSES = (
    "fresh",
    "assigned",
    "researched",
    "presented",
    "acted",
    "position",
    "closed",
    "rejected",
)
_SCOUT_ALIASES = {
    "capex": "capex",
    "debt_raise": "debt_raise",
    "debt-raise": "debt_raise",
    "insider_buying": "insider_buying",
    "insider-buying": "insider_buying",
    "orders_backlog": "orders_backlog",
    "orders-backlog": "orders_backlog",
    "accumulation": "accumulation",
}

_LEAD_INSERT = """
    INSERT INTO leads (
        lead_type, ticker, exchange, company, source_scout, run_id, source_url,
        summary, status, parent_position_id, priority, created_at, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'fresh', ?, ?, ?, ?)
"""


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    ensure_pipeline(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def ensure_pipeline(conn):
    """Create pipeline tables. Idempotent. Rebuilds a legacy leads table.

    Each migration file is applied once. A database that already has
    001_pipeline.sql still receives later files.
    """
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    ensure_history(conn)
    for name in MIGRATIONS:
        seen = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE name = ?",
            (name,),
        ).fetchone()
        if seen:
            continue
        if name == MIGRATION_NAME:
            _rename_legacy_leads(conn)
        path = HERE / "migrations" / name
        # 006 rebuilds lead_rejects while lead_reject_counterparties still
        # references it. 008 rebuilds leads and rubrics. Foreign keys are
        # off only for those scripts.
        if name in {"006_agent_handoff.sql", "008_macro_leads.sql"}:
            conn.execute("PRAGMA foreign_keys = OFF")
        conn.executescript(path.read_text(encoding="utf-8"))
        if name in {"006_agent_handoff.sql", "008_macro_leads.sql"}:
            conn.execute("PRAGMA foreign_keys = ON")
        with conn:
            if name == MIGRATION_NAME:
                _copy_legacy_leads(conn)
            conn.execute(
                "INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)",
                (name, utc_now()),
            )
    conn.execute("PRAGMA foreign_keys = ON")


def lodge_lead(
    conn,
    lead_type,
    ticker,
    exchange,
    source_scout,
    run_id,
    figure_ids=None,
    company=None,
    source_url=None,
    summary=None,
    parent_position_id=None,
    priority=None,
    created_at=None,
):
    """Insert one fresh lead. parent_position_id flags an update to a position.

    The parent row must already have status acted or position. Figure ids are
    stored in lead_figures. Returns the new lead_id.
    """
    if lead_type not in LEAD_TYPES:
        raise ValueError(f"lead_type must be one of {', '.join(LEAD_TYPES)}")
    ticker = _required_text(ticker, "ticker")
    exchange = _required_text(exchange, "exchange")
    source_scout = _required_text(source_scout, "source_scout")
    run_id = _required_text(run_id, "run_id")
    parent_position_id = _optional_int(parent_position_id, "parent_position_id")
    priority = _optional_int(priority, "priority")
    when = created_at or utc_now()
    ids = _figure_ids(figure_ids)
    with conn:
        cur = conn.execute(
            _LEAD_INSERT,
            (
                lead_type,
                ticker,
                exchange,
                company,
                source_scout,
                run_id,
                source_url,
                summary,
                parent_position_id,
                priority,
                when,
                when,
            ),
        )
        lead_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO lead_figures (lead_id, figure_id) VALUES (?, ?)",
            [(lead_id, figure_id) for figure_id in ids],
        )
    return lead_id


def record_criterion_result(
    conn,
    lead_id,
    criterion_id,
    passed,
    assessed_by,
    evidence=None,
    source_url=None,
    assessed_at=None,
):
    """Upsert one yes/no result and recompute quality_score. Returns the score."""
    lead_id = _required_int(lead_id, "lead_id")
    criterion_id = _required_int(criterion_id, "criterion_id")
    assessed_by = _required_text(assessed_by, "assessed_by")
    bit = _bit(passed)
    when = assessed_at or utc_now()
    with conn:
        conn.execute(
            """
            INSERT INTO lead_criteria_results (
                lead_id, criterion_id, passed, evidence, source_url, assessed_by, assessed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(lead_id, criterion_id) DO UPDATE SET
                passed = excluded.passed,
                evidence = excluded.evidence,
                source_url = excluded.source_url,
                assessed_by = excluded.assessed_by,
                assessed_at = excluded.assessed_at
            """,
            (lead_id, criterion_id, bit, evidence, source_url, assessed_by, when),
        )
        return _write_quality_score(conn, lead_id)


def compute_quality_score(conn, lead_id):
    """Count passed criteria, store it on the lead and its theses, and return it."""
    lead_id = _required_int(lead_id, "lead_id")
    with conn:
        return _write_quality_score(conn, lead_id)


def _write_quality_score(conn, lead_id):
    found = conn.execute(
        "SELECT 1 FROM leads WHERE lead_id = ?",
        (lead_id,),
    ).fetchone()
    if not found:
        raise ValueError(f"unknown lead_id {lead_id}")
    score = conn.execute(
        """
        SELECT COUNT(*) AS n
        FROM lead_criteria_results
        WHERE lead_id = ? AND passed = 1
        """,
        (lead_id,),
    ).fetchone()["n"]
    conn.execute(
        "UPDATE leads SET quality_score = ?, updated_at = ? WHERE lead_id = ?",
        (int(score), utc_now(), lead_id),
    )
    conn.execute(
        "UPDATE theses SET quality_score = ? WHERE lead_id = ?",
        (int(score), lead_id),
    )
    return int(score)


def _rename_legacy_leads(conn):
    legacy = _table_exists(conn, "leads_legacy")
    if not _table_exists(conn, "leads"):
        return
    if "lead_id" in _column_names(conn, "leads"):
        return
    if legacy:
        raise RuntimeError("leads_legacy already exists and leads is still the old shape")
    conn.execute("ALTER TABLE leads RENAME TO leads_legacy")


def _copy_legacy_leads(conn):
    if not _table_exists(conn, "leads_legacy"):
        return
    new_n = conn.execute("SELECT COUNT(*) AS n FROM leads").fetchone()["n"]
    old_n = conn.execute("SELECT COUNT(*) AS n FROM leads_legacy").fetchone()["n"]
    if new_n and new_n != old_n:
        raise RuntimeError(
            f"cannot copy legacy leads: new table has {new_n} rows and legacy has {old_n}"
        )
    if new_n == 0 and old_n:
        rows = conn.execute(_legacy_select(conn)).fetchall()
        payload = []
        for row in rows:
            lead_type = _lead_type_for_scout(row["scout"])
            if lead_type is None:
                raise ValueError(
                    "legacy lead "
                    f"{row['ticker']!r} on run {row['run_id']!r} has scout {row['scout']!r}, "
                    "which is not a lead type; rows remain in leads_legacy"
                )
            scout = row["scout"]
            if scout is None or not str(scout).strip():
                raise ValueError(
                    f"legacy lead {row['ticker']!r} on run {row['run_id']!r} has no scout"
                )
            when = row["started_at"] or utc_now()
            payload.append(
                (
                    lead_type,
                    row["ticker"],
                    None,
                    None,
                    str(scout).strip(),
                    row["run_id"],
                    None,
                    _legacy_summary(row["rationale"], row["status"]),
                    None,
                    row["rank"],
                    when,
                    when,
                )
            )
        conn.executemany(_LEAD_INSERT, payload)
    conn.execute("DROP TABLE leads_legacy")


def _legacy_select(conn):
    cols = _column_names(conn, "leads_legacy")
    if "run_id" not in cols or "ticker" not in cols:
        raise ValueError("legacy leads is missing run_id or ticker")

    def col(name):
        if name in cols:
            return f"l.{name} AS {name}"
        return f"NULL AS {name}"

    return f"""
        SELECT {col("run_id")}, {col("ticker")}, {col("rank")},
               {col("status")}, {col("rationale")},
               r.scout AS scout, r.started_at AS started_at
        FROM leads_legacy AS l
        LEFT JOIN runs AS r ON r.run_id = l.run_id
    """


def _legacy_summary(rationale, status):
    parts = []
    if rationale is not None and str(rationale) != "":
        parts.append(str(rationale))
    if status is not None and str(status) != "":
        parts.append(f"legacy status: {status}")
    if not parts:
        return None
    return "\n".join(parts)


def _lead_type_for_scout(scout):
    if scout is None:
        return None
    key = str(scout).strip()
    if not key:
        return None
    folded = key.casefold()
    if folded in _SCOUT_ALIASES:
        return _SCOUT_ALIASES[folded]
    underscored = folded.replace("-", "_")
    if underscored in LEAD_TYPES:
        return underscored
    return None


def _table_exists(conn, name):
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        is not None
    )


def _column_names(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _required_text(value, label):
    if value is None or not str(value).strip():
        raise ValueError(f"{label} is required")
    return str(value).strip()


def _required_int(value, label):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value


def _optional_int(value, label):
    if value is None:
        return None
    return _required_int(value, label)


def _bit(value):
    if value is True or value == 1:
        return 1
    if value is False or value == 0:
        return 0
    raise ValueError("passed must be 0 or 1")


def _figure_ids(figure_ids):
    if not figure_ids:
        return []
    ids = []
    seen = set()
    for figure_id in figure_ids:
        figure_id = _required_int(figure_id, "figure_id")
        if figure_id in seen:
            continue
        seen.add(figure_id)
        ids.append(figure_id)
    return ids
