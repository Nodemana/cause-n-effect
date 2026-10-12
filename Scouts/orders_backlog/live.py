"""Live orders-backlog pull. Writes figures into the shared SQLite history.

    python3 -m Scouts.orders_backlog.live --db /tmp/cause-n-effect.sqlite
    python3 -m Scouts.orders_backlog.live --db /tmp/cause-n-effect.sqlite --sec-fixture Scouts/orders_backlog/fixtures/amat_facts.json --no-asx

SEC companyfacts and 8-K exhibit 99 run only when SEC_CONTACT_EMAIL is set.
ASX order and backlog announcements still run. The command exits nonzero
when this run stores no figure.
"""

import json
import os
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[1]
for _entry in (str(_REPO), str(_HERE)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

import fetcher  # noqa: E402
import pipeline  # noqa: E402
from shared.store import connect, dedupe_figures, save_run, utc_now  # noqa: E402

DEFAULT_DB = Path("/tmp/cause-n-effect-orders.sqlite")
EXHIBIT_DAYS = 120
EXHIBITS_PER_COMPANY = 3


def sec_block_message():
    if os.environ.get("SEC_CONTACT_EMAIL", "").strip():
        return None
    return "SEC_CONTACT_EMAIL is missing. No SEC request was sent."


def _option(args, name):
    if name not in args:
        return None
    index = args.index(name)
    if index + 1 >= len(args):
        raise SystemExit(f"{name} needs a value")
    return args[index + 1]


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    db_path = Path(_option(args, "--db") or DEFAULT_DB)
    customers = fetcher.load_customers()
    suppliers = fetcher.load_suppliers()
    customer_facts = []
    supplier_facts = []
    releases = []
    fixture = _option(args, "--sec-fixture")
    if fixture:
        fixture = Path(fixture)
        payload = json.loads(fixture.read_text(encoding="utf-8"))
        company = _company_for_facts(payload, customers + suppliers) or customers[0]
        side = "supplier_order" if company in suppliers else "customer_backlog"
        bucket = supplier_facts if side == "supplier_order" else customer_facts
        bucket.append((company, payload))
        print(f"SEC fixture {fixture}")
    else:
        blocked = sec_block_message()
        if blocked:
            print(blocked)
        else:
            print(f"SEC companyfacts for {len(customers)} customers and {len(suppliers)} suppliers")
            customer_facts.extend(_pairs(fetcher.collect_customer_facts(customers)))
            supplier_facts.extend(_pairs(fetcher.collect_supplier_facts(suppliers)))
            since = (date.today() - timedelta(days=EXHIBIT_DAYS)).isoformat()
            print(f"SEC 8-K exhibits since {since}, at most {EXHIBITS_PER_COMPANY} per company")
            releases.extend(
                _releases(
                    fetcher.collect_8k_exhibits(
                        customers,
                        items=fetcher._8K_CUSTOMER_ITEMS,
                        since=since,
                        limit_per_company=EXHIBITS_PER_COMPANY,
                    ),
                    "customer_backlog",
                )
            )
            releases.extend(
                _releases(
                    fetcher.collect_8k_exhibits(
                        suppliers,
                        items=fetcher._8K_SUPPLIER_ITEMS,
                        since=since,
                        limit_per_company=EXHIBITS_PER_COMPANY,
                    ),
                    "supplier_order",
                )
            )
    if "--no-asx" not in args:
        print(f"ASX order and backlog announcements, at most {fetcher.ASX_CAP} per company")
        releases.extend(_releases(fetcher.collect_asx(fetcher.load_asx()), "supplier_order"))
    table = pipeline.build_table(customer_facts, supplier_facts, releases)
    started = utc_now()
    seen = started[:10]
    figures = dedupe_figures(_figures(table["records"], seen))
    if not figures:
        print("No orders-backlog figures.")
        return 1
    conn = connect(db_path)
    try:
        run_id = save_run(
            conn,
            "orders-backlog",
            started,
            figures,
            notes=f"records={len(table['records'])}",
        )
    finally:
        conn.close()
    print(f"run {run_id} figures {len(figures)}")
    _print_rows(db_path, run_id)
    return 0


def _company_for_facts(payload, companies):
    raw = str(payload.get("cik") or "")
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return None
    cik = digits.zfill(10)
    for company in companies:
        if "".join(ch for ch in str(company.get("cik") or "") if ch.isdigit()).zfill(10) == cik:
            return company
    return None


def _pairs(found):
    return [(company, payload) for company, payload, _url in found]


def _releases(documents, source_side):
    rows = []
    for document in documents:
        rows.append(
            {
                "company": document["company"],
                "body": document.get("body") or "",
                "url": document.get("url") or "",
                "filing_date": document.get("filing_date"),
                "source_side": source_side,
                "source_type": "asx_announcement" if document.get("form") == "ASX" else "filing",
            }
        )
    return rows


def _figures(records, seen):
    rows = []
    for record in records:
        if record.get("value") is None or not record.get("ticker"):
            continue
        notes = record.get("notes") or []
        concept = notes[0] if notes and " " not in notes[0] else (record.get("disclosure") or "")
        rows.append(
            {
                "ticker": record["ticker"],
                "company": record.get("company"),
                "figure_type": record.get("source_side") or "backlog",
                "instrument": concept,
                "period": f"{record.get('equipment_or_period') or ''}|{record.get('filing_date') or ''}",
                "value": record["value"],
                "value_text": str(record["value"]),
                "currency": record.get("currency"),
                "seen_at": seen,
                "announced_on": (record.get("filing_date") or "")[:10] or None,
                "source_url": record.get("source_url"),
                "source_type": "asx_announcement" if record.get("source_type") == "asx_announcement" else "filing",
                "verified": 0 if record.get("flags") else 1,
            }
        )
    return rows


def _print_rows(db_path, run_id):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    stored = conn.execute(
        """
        SELECT ticker, figure_type, instrument, period, value, currency, verified, source_type
        FROM figures WHERE run_id = ? ORDER BY ticker, period
        """,
        (run_id,),
    ).fetchall()
    conn.close()
    for row in stored:
        print(
            f"  {row['ticker']} {row['figure_type']} {row['instrument']} "
            f"period={row['period']} value={row['value']} {row['currency'] or ''} "
            f"verified={row['verified']} source_type={row['source_type']}"
        )
    print(f"db {db_path}")


if __name__ == "__main__":
    sys.exit(main())
