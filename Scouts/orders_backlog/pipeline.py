"""Build the single orders-backlog table from facts and release text."""

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_REPO = _ROOT.parents[1]
for _entry in (str(_REPO), str(_ROOT)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from parser import parse_company_facts, parse_release
from shared.store import connect, dedupe_figures, save_run

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


def build_table(customer_facts=(), supplier_facts=(), releases=()):
    """Return one table. Both vantage points use the same columns.

    `customer_facts` and `supplier_facts` are (company, payload) pairs.
    `releases` are dicts with company, body, url, filing_date, source_side,
    and source_type.
    """
    rows = []
    for company, payload in customer_facts:
        rows.extend(parse_company_facts(payload, company, "customer_backlog"))
    for company, payload in supplier_facts:
        rows.extend(parse_company_facts(payload, company, "supplier_order"))
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


def figures_from_records(records, seen_at):
    """History rows for one orders-backlog table. Flagged rows stay, unverified."""
    figures = []
    for row in records:
        ticker = row.get("ticker")
        if not ticker:
            continue
        filed = str(row.get("filing_date") or "")[:10]
        figures.append(
            {
                "ticker": ticker,
                "company": row.get("company"),
                "figure_type": _figure_type(row),
                "instrument": row.get("counterparty") or "",
                "period": row.get("equipment_or_period") or "",
                "value": row.get("value"),
                "value_text": _plain(row.get("value")),
                "currency": row.get("currency"),
                "seen_at": seen_at,
                "announced_on": filed or None,
                "source_url": row.get("source_url"),
                "source_type": row.get("source_type"),
                "verified": 0 if row.get("flags") else 1,
                "start": row.get("start"),
                "end": row.get("end"),
                "frame": row.get("frame"),
                "fp": row.get("fp"),
            }
        )
    return figures


def store_run(db_path, records, started_at, seen_at, notes=None):
    """Dedupe, then write one orders-backlog run. Duplicate keys are logged."""
    figures = dedupe_figures(figures_from_records(records, seen_at))
    run_id = f"{started_at[:10]}-orders-backlog"
    conn = connect(db_path)
    try:
        save_run(conn, run_id, "orders-backlog", started_at, figures, notes=notes)
    finally:
        conn.close()
    return {"run_id": run_id, "figures": len(figures)}


def _figure_type(row):
    for note in reversed(row.get("notes") or []):
        if (
            isinstance(note, str)
            and note.isidentifier()
            and note[:1].isupper()
            and not note.isupper()
        ):
            return note
    return row.get("disclosure") or "backlog"


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
