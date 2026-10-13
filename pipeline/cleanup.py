"""Remove figures and orphaned lead rows that should not stay in the database.

`cleanup_database` plans the removals, and with apply=False (the default)
it only returns the counts. With apply=True it copies the database, then
deletes inside one transaction and writes cleanup_log. A second apply finds
nothing.

Rules, in order, so a row is removed once:

- unofficial_source: the source URL host is not SEC, ASX, or Markit
- prior_year_comparative: a 10-Q row whose period ends in a year before the filing
- implausible_figure: the company-total backlog filter already used by the sweep
- duplicate_figure: same ticker, type, instrument, period, and value (a tiny
  relative gap counts as the same value); keep the earliest official source
- superseded_figure: same key with a different value; keep the latest filing
- orphan_lead / orphan_thesis: a lead with no remaining figures, or a thesis
  whose lead is gone
"""

import sqlite3
from pathlib import Path

from pipeline.load import ORDER_TYPES, period_end
from pipeline.prefilter import _implausible_total
from shared.pipeline_store import connect
from shared.store import utc_now

REASONS = (
    "unofficial_source",
    "prior_year_comparative",
    "implausible_figure",
    "duplicate_figure",
    "superseded_figure",
    "orphan_lead",
    "orphan_thesis",
)
_NEAR = 1e-6
_OFFICIAL_TYPES = {"filing", "company_release", "asx_announcement", "xbrl_company_facts"}


def cleanup_database(db_path, *, apply=False, now=None):
    """Return a summary keyed by reason. apply=False writes nothing."""
    when = now or utc_now()
    conn = connect(db_path)
    try:
        plan = plan_cleanup(conn)
        summary = {reason: 0 for reason in REASONS}
        for item in plan:
            summary[item["reason"]] += 1
        backup = None
        if apply and plan:
            conn.commit()
            backup = backup_database(conn, db_path, when)
            _apply(conn, plan, when)
        return {"summary": summary, "backup": backup, "apply": bool(apply), "removed": len(plan) if apply else 0}
    finally:
        conn.close()


def plan_cleanup(conn):
    rows = conn.execute(
        """
        SELECT f.figure_id, f.run_id, f.ticker, f.figure_type, f.instrument, f.period,
               f.value, f.announced_on, f.source_url, f.source_type, r.started_at
        FROM figures AS f
        JOIN runs AS r ON r.run_id = f.run_id
        """
    ).fetchall()
    claimed = set()
    plan = []

    def claim(row, reason):
        figure_id = row["figure_id"]
        if figure_id in claimed:
            return
        claimed.add(figure_id)
        plan.append(
            {
                "table": "figures",
                "row_id": figure_id,
                "reason": reason,
                "run_id": row["run_id"],
            }
        )

    for row in rows:
        if _unofficial(row):
            claim(row, "unofficial_source")
    for row in rows:
        if _prior_year(row):
            claim(row, "prior_year_comparative")
    for row in rows:
        if row["figure_id"] in claimed:
            continue
        if row["figure_type"] not in ORDER_TYPES:
            continue
        if _implausible_total(conn, _View(row), "orders_backlog"):
            claim(row, "implausible_figure")
    for loser in _duplicate_losers(rows, claimed):
        claim(loser, "duplicate_figure")
    for loser in _superseded_losers(rows, claimed):
        claim(loser, "superseded_figure")
    plan.extend(_orphan_plan(conn, {item["row_id"] for item in plan if item["table"] == "figures"}))
    return plan


def backup_database(conn, db_path, now):
    """Copy the database next to itself before any delete."""
    source = Path(db_path)
    stamp = str(now).replace(":", "").replace("-", "")
    dest = source.with_name(source.name + f".backup-{stamp}")
    if dest.exists():
        dest = source.with_name(source.name + f".backup-{stamp}-{dest.stat().st_mtime_ns}")
    copied = sqlite3.connect(dest)
    try:
        conn.backup(copied)
    finally:
        copied.close()
    return str(dest)


def _apply(conn, plan, now):
    conn.commit()
    try:
        conn.execute("BEGIN")
        for item in plan:
            _delete_row(conn, item)
            conn.execute(
                """
                INSERT INTO cleanup_log (row_id, table_name, reason, run_id, logged_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (item["row_id"], item["table"], item["reason"], item["run_id"], now),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _delete_row(conn, item):
    if item["reason"] == "orphan_thesis":
        _delete_thesis(conn, item["row_id"])
        return
    if item["reason"] == "orphan_lead":
        _delete_lead(conn, item["row_id"])
        return
    conn.execute("DELETE FROM figures WHERE figure_id = ?", (item["row_id"],))


def _delete_lead(conn, lead_id):
    found = conn.execute("SELECT 1 FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
    if not found:
        return
    conn.execute(
        "UPDATE leads SET parent_position_id = NULL WHERE parent_position_id = ?",
        (lead_id,),
    )
    for table in (
        "lead_criteria_results",
        "lead_counterparties",
        "rankings",
        "decisions",
        "legate_selections",
        "legate_merge_leads",
    ):
        conn.execute(f"DELETE FROM {table} WHERE lead_id = ?", (lead_id,))
    merges = conn.execute(
        "SELECT merge_id FROM legate_merges WHERE survivor_lead_id = ?",
        (lead_id,),
    ).fetchall()
    for merge in merges:
        conn.execute("DELETE FROM legate_merge_leads WHERE merge_id = ?", (merge["merge_id"],))
        conn.execute("DELETE FROM legate_merges WHERE merge_id = ?", (merge["merge_id"],))
    theses = conn.execute("SELECT thesis_id FROM theses WHERE lead_id = ?", (lead_id,)).fetchall()
    for thesis in theses:
        _delete_thesis(conn, thesis["thesis_id"])
    conn.execute("DELETE FROM leads WHERE lead_id = ?", (lead_id,))


def _delete_thesis(conn, thesis_id):
    conn.execute("DELETE FROM praetorian_reviews WHERE thesis_id = ?", (thesis_id,))
    conn.execute("DELETE FROM critiques WHERE thesis_id = ?", (thesis_id,))
    conn.execute("DELETE FROM theses WHERE thesis_id = ?", (thesis_id,))


def _orphan_plan(conn, deleted_figures):
    plan = []
    leads = conn.execute("SELECT lead_id, run_id FROM leads").fetchall()
    orphan_leads = set()
    for lead in leads:
        links = [
            row["figure_id"]
            for row in conn.execute(
                "SELECT figure_id FROM lead_figures WHERE lead_id = ?",
                (lead["lead_id"],),
            )
        ]
        if links and any(figure_id not in deleted_figures for figure_id in links):
            continue
        orphan_leads.add(lead["lead_id"])
        plan.append(
            {
                "table": "leads",
                "row_id": lead["lead_id"],
                "reason": "orphan_lead",
                "run_id": lead["run_id"],
            }
        )
    theses = conn.execute("SELECT thesis_id, lead_id, run_id FROM theses").fetchall()
    live_leads = {lead["lead_id"] for lead in leads}
    for thesis in theses:
        lead_id = thesis["lead_id"]
        if lead_id in orphan_leads or lead_id not in live_leads:
            plan.append(
                {
                    "table": "theses",
                    "row_id": thesis["thesis_id"],
                    "reason": "orphan_thesis",
                    "run_id": thesis["run_id"],
                }
            )
    return plan


def _duplicate_losers(rows, claimed):
    groups = {}
    for row in rows:
        if row["figure_id"] in claimed or row["value"] is None:
            continue
        key = (row["ticker"], row["figure_type"], row["instrument"] or "", row["period"] or "")
        groups.setdefault(key, []).append(row)
    losers = []
    for group in groups.values():
        clusters = []
        for row in group:
            placed = False
            for cluster in clusters:
                if _near(row["value"], cluster[0]["value"]):
                    cluster.append(row)
                    placed = True
                    break
            if not placed:
                clusters.append([row])
        for cluster in clusters:
            if len(cluster) < 2:
                continue
            cluster.sort(key=_duplicate_rank)
            losers.extend(cluster[1:])
    return losers


def _superseded_losers(rows, claimed):
    groups = {}
    for row in rows:
        if row["figure_id"] in claimed or row["value"] is None:
            continue
        key = (row["ticker"], row["figure_type"], row["instrument"] or "", row["period"] or "")
        groups.setdefault(key, []).append(row)
    losers = []
    for group in groups.values():
        distinct = []
        for row in group:
            if any(_near(row["value"], kept["value"]) for kept in distinct):
                continue
            distinct.append(row)
        if len(distinct) < 2:
            continue
        distinct.sort(key=_filing_rank)
        losers.extend(distinct[:-1])
    return losers


def _duplicate_rank(row):
    """Earliest official source, then the earliest run, then the lowest id."""
    official = 0 if _official(row) else 1
    return (official, row["started_at"] or "", row["announced_on"] or "", row["figure_id"])


def _filing_rank(row):
    return (row["announced_on"] or "", row["started_at"] or "", row["figure_id"])


def _near(left, right):
    if left == right:
        return True
    scale = max(abs(left), abs(right), 1.0)
    return abs(left - right) / scale <= _NEAR


def _unofficial(row):
    url = (row["source_url"] or "").strip()
    if not url:
        return False
    return not _official_host(url)


def _official(row):
    if (row["source_type"] or "") in _OFFICIAL_TYPES:
        return True
    return _official_host(row["source_url"] or "")


def _official_host(url):
    host = _host(url)
    if host == "sec.gov" or host.endswith(".sec.gov"):
        return True
    if host == "asx.com.au" or host.endswith(".asx.com.au"):
        return True
    if host == "asx.api.markitdigital.com":
        return True
    return False


def _host(url):
    from urllib.parse import urlsplit

    return (urlsplit(url).hostname or "").lower().rstrip(".")


def _prior_year(row):
    blob = " ".join(
        (
            row["source_type"] or "",
            row["source_url"] or "",
            row["instrument"] or "",
        )
    ).casefold()
    if "10-q" not in blob and "10q" not in blob:
        return False
    end = period_end(row["period"])
    filed = row["announced_on"] or row["started_at"] or ""
    if end is None or len(filed) < 4:
        return False
    try:
        filed_year = int(str(filed)[:4])
    except ValueError:
        return False
    return end.year < filed_year


class _View:
    def __init__(self, row):
        self.ticker = row["ticker"]
        self.figure_id = row["figure_id"]
        self.value = row["value"]
        self.instrument = row["instrument"]
