"""Build the single orders-backlog table from facts and release text."""

import json
import re
import sys
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_REPO = _ROOT.parents[1]
for _entry in (str(_REPO), str(_ROOT)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from Scouts.orders_backlog.parser import (  # noqa: E402
    DEFAULT_RECENT_QUARTERS,
    _recent_periods,
    _row_period_end,
    parse_company_facts,
    parse_release,
)
from Scouts.orders_backlog.tags import ACTIVE_BACKLOG_TAGS, STALE_BACKLOG_TAGS  # noqa: E402
from shared.store import connect, dedupe_figures, save_run  # noqa: E402

# A latest period older than this, measured from the run date, is called out.
DEFAULT_STALE_DAYS = 180

# A company-total backlog smaller than this share of the same company's other
# total is not a second total. It is a misread award, cash-flow, or segment figure.
_IMPLAUSIBLE_RATIO = 0.10
_NEAR_DAYS = 7
_NEAR_AMOUNT = 0.02
_COMPANY_BACKLOG = {"backlog", *ACTIVE_BACKLOG_TAGS, *STALE_BACKLOG_TAGS}
_REVENUE = {"revenue", "revenues"}

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
                accession=release.get("accession"),
            )
        )
    rows = drop_same_quarter_duplicates(rows)
    rows = _recent_periods(rows, max_quarters)
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
    figures = figures_from_records(table.get("records") or [], seen_at)
    # A sentence that names backlog without an amount shares the unique key.
    # The valued row has to be seen first so a tie keeps the amount.
    figures.sort(key=lambda row: row.get("value") is None)
    figures = dedupe_figures(keep_distinct_amounts(drop_implausible_totals(figures)))
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


def drop_implausible_totals(figures, ratio=_IMPLAUSIBLE_RATIO):
    """Drop a company-total backlog that is under 10% of a larger total.

    The larger total is another company backlog or a revenue figure for the
    same ticker. Segment rows stay. The skip is printed.
    """
    backlog = {}
    revenue = {}
    for row in figures or []:
        value = _amount(row)
        if value is None:
            continue
        ticker = row.get("ticker") or ""
        if _is_company_backlog(row):
            backlog[ticker] = max(backlog.get(ticker, 0.0), value)
        elif _is_revenue(row):
            revenue[ticker] = max(revenue.get(ticker, 0.0), value)
    kept = []
    for row in figures or []:
        if _is_company_backlog(row):
            value = _amount(row)
            ticker = row.get("ticker") or ""
            ceiling = max(backlog.get(ticker, 0.0), revenue.get(ticker, 0.0))
            if value is not None and ceiling and value < ceiling * float(ratio):
                print(
                    f"skipped implausible backlog ticker={row.get('ticker')} "
                    f"value={value:.0f} prior={ceiling:.0f} period={row.get('period') or ''}"
                )
                continue
        kept.append(row)
    return kept


def drop_same_quarter_duplicates(rows, days=_NEAR_DAYS, tolerance=_NEAR_AMOUNT):
    """Keep one row when an 8-K repeats a companyfacts total.

    Same ticker, same metric or segment, period ends within 7 days, and
    amounts within 2%. The companyfacts row wins. The dropped row is logged.
    """
    kept = []
    for row in rows or []:
        match = None
        for prior in kept:
            if _same_quarter_metric(row, prior, days, tolerance):
                match = prior
                break
        if match is None:
            kept.append(row)
            continue
        winner = match if _prefer_companyfacts(match, row) else row
        loser = row if winner is match else match
        if winner is not match:
            kept[kept.index(match)] = winner
        value = loser.get("value")
        if value is not None:
            print(
                f"skipped duplicate backlog ticker={loser.get('ticker')} "
                f"value={float(value):.0f} period={loser.get('equipment_or_period') or ''} "
                f"kept={winner.get('source_type') or ''} period={winner.get('equipment_or_period') or ''}"
            )
    return kept


def _same_quarter_metric(left, right, days, tolerance):
    if (left.get("ticker") or "") != (right.get("ticker") or ""):
        return False
    key_left = _metric_key(left)
    key_right = _metric_key(right)
    if not key_left or key_left != key_right:
        return False
    amount_left = left.get("value")
    amount_right = right.get("value")
    if amount_left is None or amount_right is None:
        return False
    amount_left = float(amount_left)
    amount_right = float(amount_right)
    ceiling = max(abs(amount_left), abs(amount_right))
    if not ceiling or abs(amount_left - amount_right) / ceiling > float(tolerance):
        return False
    end_left = _row_period_end(left)
    end_right = _row_period_end(right)
    if end_left is None or end_right is None:
        return False
    return abs((end_left - end_right).days) <= int(days)


def _metric_key(row):
    disclosure = (row.get("disclosure") or "").strip()
    if ":" in disclosure:
        return disclosure.casefold()
    if disclosure in {"backlog", "order_intake"}:
        return disclosure
    return None


def _prefer_companyfacts(left, right):
    left_facts = left.get("source_type") == "xbrl_company_facts"
    right_facts = right.get("source_type") == "xbrl_company_facts"
    if left_facts != right_facts:
        return left_facts
    return (left.get("filing_date") or "") >= (right.get("filing_date") or "")


def _is_company_backlog(row):
    instrument = row.get("instrument") or ""
    if ":" in instrument:
        return False
    return instrument in _COMPANY_BACKLOG


def _is_revenue(row):
    instrument = (row.get("instrument") or "").split(":", 1)[0].casefold()
    return instrument in _REVENUE


def _amount(row):
    value = row.get("value")
    if value is None:
        return None
    return float(value)


def keep_distinct_amounts(figures):
    """One row per metric and segment. The amount is not written into the label.

    An unlabeled orders or backlog row is dropped when a segmented row has the
    same amount. A second copy whose period is only the filing date is dropped
    when the quarter-end row is already there. Company-facts rows are unchanged,
    so two currencies for one concept still collapse on the shared key.
    """
    rows = list(figures or [])
    segmented = set()
    for row in rows:
        instrument = row.get("instrument") or ""
        if ":" not in instrument:
            continue
        kind = instrument.split(":", 1)[0]
        segmented.add((
            row.get("ticker") or "",
            row.get("figure_type") or "",
            kind,
            round(float(row.get("value") or 0), 2),
        ))
    kept = []
    for row in rows:
        instrument = row.get("instrument") or ""
        if instrument in {"orders", "backlog", "revenue"}:
            kind = instrument
            mark = (
                row.get("ticker") or "",
                row.get("figure_type") or "",
                kind,
                round(float(row.get("value") or 0), 2),
            )
            if mark in segmented:
                continue
        kept.append(row)
    grouped = {}
    order = []
    for row in kept:
        instrument = row.get("instrument") or ""
        prose = ":" in instrument or instrument in {"orders", "backlog", "revenue"}
        if not prose:
            order.append(row)
            continue
        key = (
            row.get("ticker") or "",
            row.get("figure_type") or "",
            instrument,
            round(float(row.get("value") or 0), 2),
        )
        grouped.setdefault(key, []).append(row)
    result = list(order)
    for group in grouped.values():
        quarter = [
            row
            for row in group
            if (row.get("period") or "")[:10] != (row.get("announced_on") or "")[:10]
        ]
        result.extend(quarter or group)
    return result


def stale_latest_periods(records, as_of, max_age_days=DEFAULT_STALE_DAYS):
    """Tickers whose newest stored period ends more than `max_age_days` before `as_of`.

    A sentence that names a date but not an amount is not a period. The rows
    that do have a value stay in the table. The caller prints the stale ones.
    """
    as_of = _as_of(as_of)
    latest = {}
    for record in records or []:
        ticker = record.get("ticker")
        if record.get("value") is None:
            continue
        end = _period_end(record.get("equipment_or_period"))
        if not ticker or end is None:
            continue
        current = latest.get(ticker)
        if current is None or end > current[0]:
            latest[ticker] = (end, record)
    stale = []
    for ticker, (end, record) in sorted(latest.items()):
        age = (as_of - end).days
        if age > int(max_age_days):
            stale.append({"ticker": ticker, "period": end.isoformat(), "age_days": age, "record": record})
    return stale


def flag_stale_latest(records, as_of, max_age_days=DEFAULT_STALE_DAYS):
    """Mark each stale latest row and return the tickers for the run output."""
    stale = stale_latest_periods(records, as_of, max_age_days)
    for item in stale:
        record = item["record"]
        flags = record.setdefault("flags", [])
        if "stale_period" not in flags:
            flags.append("stale_period")
        notes = record.setdefault("notes", [])
        note = f"stale latest period {item['period']}"
        if note not in notes:
            notes.append(note)
    return stale


def report_stale_latest(records, as_of, max_age_days=DEFAULT_STALE_DAYS, stream=None):
    """Print one line per ticker whose latest period is stale. Returns those rows."""
    stale = flag_stale_latest(records, as_of, max_age_days)
    out = stream or sys.stdout
    for item in stale:
        print(
            f"stale latest period ticker={item['ticker']} period={item['period']} age_days={item['age_days']}",
            file=out,
        )
    return stale


def _as_of(value):
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _period_end(value):
    text = (value or "").strip()
    if "|" in text:
        text = text.split("|", 1)[0].strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return date.fromisoformat(text)
    return None


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
