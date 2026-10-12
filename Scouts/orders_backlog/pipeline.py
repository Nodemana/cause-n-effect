"""Build the single orders-backlog table from facts and release text."""

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_REPO = _ROOT.parents[1]
for _entry in (str(_REPO), str(_ROOT)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from parser import DEFAULT_RECENT_QUARTERS, parse_company_facts, parse_release  # noqa: E402
from shared.store import connect, dedupe_figures, save_run  # noqa: E402

_RECORD_KEYS = (
    "source_side",
    "company",
    "ticker",
    "counterparty",
    "equipment_or_period",
    "value",
    "currency",
    "timing",
    "capacity",
    "source_url",
    "filing_date",
    "source_type",
    "disclosure",
    "flags",
    "notes",
)


def build_table(customer_facts=(), supplier_facts=(), releases=(), max_quarters=DEFAULT_RECENT_QUARTERS):
    """Return one table. Both vantage points use the same columns.

    `customer_facts` and `supplier_facts` are (company, payload) pairs.
    `releases` are dicts with company, body, url, filing_date, source_side,
    and source_type.
    """
    rows = []
    for company, payload in customer_facts:
        rows.extend(parse_company_facts(payload, company, "customer_backlog", max_quarters=max_quarters))
    for company, payload in supplier_facts:
        rows.extend(parse_company_facts(payload, company, "supplier_order", max_quarters=max_quarters))
    for release in releases:
        rows.extend(
            parse_release(
                release.get("body") or "",
                release["company"],
                release["source_side"],
                release.get("source_type") or "release",
                release.get("url") or "",
                release.get("filing_date"),
            )
        )
    rows.sort(
        key=lambda row: (
            row.get("filing_date") or "",
            row.get("ticker") or "",
            row.get("source_side") or "",
            row.get("disclosure") or "",
            row.get("source_url") or "",
        )
    )
    return {"scout": "orders-backlog", "schema": 1, "records": rows}


def to_json(table):
    return json.dumps(table, indent=2) + "\n"


def store_run(db_path, table, started_at, notes=None):
    """Insert one orders-backlog run. Earlier runs and their figures stay put.

    Rows that share a figures key are collapsed first. A single-quarter value
    beats a year-to-date value, then the latest filing wins. Drops are logged.
    """
    seen_at = started_at[:10]
    figures = dedupe_figures(figures_from_records(table.get("records") or [], seen_at))
    conn = connect(db_path)
    try:
        run_id = save_run(conn, "orders-backlog", started_at, figures, notes=notes)
    finally:
        conn.close()
    return {"run_id": run_id, "figures": len(figures)}


def figures_from_records(records, seen_at):
    rows = []
    for record in records:
        ticker = record.get("ticker")
        if not ticker:
            continue
        value = record.get("value")
        filed = record.get("filing_date") or ""
        rows.append(
            {
                "ticker": ticker,
                "company": record.get("company"),
                "figure_type": record["source_side"],
                "instrument": record.get("counterparty") or record.get("disclosure") or "",
                "period": record.get("equipment_or_period") or filed,
                "value": value,
                "value_text": _plain(value),
                "currency": record.get("currency"),
                "seen_at": seen_at,
                "announced_on": filed[:10] or None,
                "source_url": record.get("source_url"),
                "source_type": record.get("source_type"),
                "verified": 0 if record.get("flags") else 1,
                "start": record.get("start"),
                "end": record.get("end"),
                "frame": record.get("frame"),
                "fp": record.get("fp"),
            }
        )
    return rows


def _plain(value):
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def assert_shape(row):
    missing = [key for key in _RECORD_KEYS if key not in row]
    if missing:
        raise KeyError(",".join(missing))
