"""Live orders-backlog pull. Writes figures into the shared SQLite history.

SEC company facts are requested only when SEC_CONTACT_EMAIL is set. When it
is missing, this script stops before any SEC request. Pass a fixture to store
without contacting SEC:

    python3 -m Scouts.orders_backlog.live --db /tmp/cause-n-effect.sqlite --sec-fixture Scouts/orders_backlog/fixtures/amat_facts.json --no-asx

The default database is /tmp/cause-n-effect-orders.sqlite. This command does
not call ASX. `--no-asx` is accepted so it can sit beside the other live pulls.
"""

import json
import os
import sqlite3
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[1]
for _entry in (str(_REPO), str(_HERE)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

import fetcher  # noqa: E402
import parser  # noqa: E402
import pipeline  # noqa: E402
from shared.store import utc_now  # noqa: E402

DEFAULT_DB = Path("/tmp/cause-n-effect-orders.sqlite")


def sec_block_message():
    if os.environ.get("SEC_CONTACT_EMAIL", "").strip():
        return None
    return "SEC_CONTACT_EMAIL is missing. No SEC request was sent."


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    db_path = DEFAULT_DB
    if "--db" in args:
        db_path = Path(args[args.index("--db") + 1])
    records = []
    if "--sec-fixture" in args:
        fixture = Path(args[args.index("--sec-fixture") + 1])
        payload = json.loads(fixture.read_text(encoding="utf-8"))
        company = _company_for(payload, fetcher.load_customers())
        records.extend(parser.parse_company_facts(payload, company, "customer_backlog"))
        print(f"SEC fixture {fixture}")
    else:
        blocked = sec_block_message()
        if blocked:
            print(blocked)
        else:
            print("SEC companyfacts for customer backlog and supplier order intake")
            records.extend(_from_pairs(fetcher.collect_customer_facts(fetcher.load_customers()), "customer_backlog"))
            records.extend(_from_pairs(fetcher.collect_supplier_facts(fetcher.load_suppliers()), "supplier_order"))
    if not records:
        print("No orders-backlog figures.")
        return 1
    started = utc_now()
    seen = started[:10]
    summary = pipeline.store_run(
        db_path,
        records,
        started_at=started,
        seen_at=seen,
        notes="SEC company facts for backlog and order intake.",
    )
    print(f"run {summary['run_id']} figures {summary['figures']}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT ticker, figure_type, instrument, period, value, value_text, currency,
               announced_on, source_url, source_type, verified
        FROM figures
        WHERE run_id = ?
        ORDER BY ticker, figure_type, period
        """,
        (summary["run_id"],),
    ).fetchall()
    if not rows:
        print("figures: (none)")
    for row in rows:
        print(
            f"  {row['ticker']} {row['figure_type']} period={row['period']} "
            f"value={row['value']} {row['currency'] or ''} text={row['value_text']} "
            f"announced_on={row['announced_on']} verified={row['verified']} "
            f"source_type={row['source_type']} url={row['source_url']}"
        )
    conn.close()
    print(f"db {db_path}")
    return 0 if rows else 1


def _from_pairs(pairs, source_side):
    records = []
    for item in pairs:
        company, payload = item[0], item[1]
        records.extend(parser.parse_company_facts(payload, company, source_side))
    return records


def _company_for(payload, companies):
    raw = payload.get("cik")
    if raw is not None and str(raw).strip():
        want = str(int(raw)).zfill(10)
        for company in companies:
            if str(int(company["cik"])).zfill(10) == want:
                return company
    return companies[0]


if __name__ == "__main__":
    sys.exit(main())
