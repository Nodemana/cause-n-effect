"""Live insider-buying pull. Writes figures into the shared SQLite history.

    python3 -m Scouts.insider_buying.live --db /tmp/cause-n-effect.sqlite

SEC Form 4 requests run only when SEC_CONTACT_EMAIL is set. Otherwise the
script says so and does not send one. ASX Appendix 3Y notices still run.
The command exits nonzero when this run stores no figure.

Offline SEC check, using the fixture XML instead of EDGAR:

    python3 -m Scouts.insider_buying.live --db /tmp/cause-n-effect.sqlite --sec-fixture Scouts/insider_buying/fixtures/sec_form4_ceo.xml --no-asx
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
from shared.store import connect, save_run, utc_now  # noqa: E402

UNIVERSE = _HERE / "universe_asx.json"
DEFAULT_DB = Path("/tmp/cause-n-effect-insider.sqlite")


def sec_block_message():
    if os.environ.get("SEC_CONTACT_EMAIL", "").strip():
        return None
    return "SEC_CONTACT_EMAIL is missing. No SEC request was sent."


def _unique_figures(figures):
    """Same-day buys that share a period get a suffix so the figures key still holds."""
    used = set()
    kept = []
    for item in figures:
        period = item["period"]
        key = (item["ticker"], item["figure_type"], item["instrument"], period)
        n = 2
        while key in used:
            period = f"{item['period']}|{n}"
            key = (item["ticker"], item["figure_type"], item["instrument"], period)
            n += 1
        item["period"] = period
        used.add(key)
        kept.append(item)
    return kept


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    db_path = DEFAULT_DB
    if "--db" in args:
        db_path = Path(args[args.index("--db") + 1])
    documents = []
    if "--sec-fixture" in args:
        fixture = Path(args[args.index("--sec-fixture") + 1])
        documents.append(
            {
                "source": "sec",
                "body": fixture.read_text(encoding="utf-8"),
                "url": fixture.as_posix(),
                "filing_date": "2026-03-04",
                "ticker": None,
                "company": None,
            }
        )
        print(f"SEC fixture {fixture}")
    else:
        blocked = sec_block_message()
        if blocked:
            print(blocked)
        else:
            end = date.today().isoformat()
            start = (date.today() - timedelta(days=7)).isoformat()
            print(f"SEC Form 4 window {start} to {end}, at most 5 filings")
            documents.extend(fetcher.collect_sec(start, end, limit=5))
    if "--no-asx" not in args:
        companies = json.loads(UNIVERSE.read_text(encoding="utf-8"))["companies"]
        print(f"ASX Appendix 3Y, at most {5} announcements considered per company")
        documents.extend(fetcher.collect_asx(companies))
    if not documents:
        print("No filings to parse.")
        return 1
    table = pipeline.build_table(documents)
    started = utc_now()
    seen = started[:10]
    figures = []
    for row in table["figures"]:
        if not row.get("ticker"):
            continue
        item = dict(row)
        item["seen_at"] = item.get("seen_at") or seen
        item["instrument"] = item.get("instrument") or ""
        item["period"] = item.get("period") or ""
        figures.append(item)
    figures = _unique_figures(figures)
    run_id = f"{seen}-insider-buying"
    notes = f"records={len(table['records'])} review={len(table['review'])}"
    conn = connect(db_path)
    try:
        save_run(conn, run_id, "insider-buying", started, figures, notes=notes)
    finally:
        conn.close()
    print(f"run {run_id} figures {len(figures)} review {len(table['review'])}")
    show = sqlite3.connect(db_path)
    show.row_factory = sqlite3.Row
    rows = show.execute(
        """
        SELECT ticker, figure_type, instrument, period, value, value_text, currency,
               announced_on, source_url, source_type, verified
        FROM figures
        WHERE run_id = ?
        ORDER BY ticker, period
        """,
        (run_id,),
    ).fetchall()
    show.close()
    if not rows:
        print("figures: (none)")
        print(f"db {db_path}")
        return 1
    for row in rows:
        print(
            f"  {row['ticker']} {row['figure_type']} {row['value_text']} "
            f"value={row['value']} {row['currency']} period={row['period']} "
            f"announced_on={row['announced_on']} verified={row['verified']} "
            f"source_type={row['source_type']} url={row['source_url']}"
        )
    print(f"db {db_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
