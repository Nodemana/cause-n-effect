"""Live debt-raise pull. Writes a gitignored SQLite file and prints what came back.

SEC is contacted only when SEC_CONTACT_EMAIL is set. When it is missing, this
script stops before any SEC request and still runs the ASX watchlist.
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

UNIVERSE = _HERE / "universe_asx.json"
DEFAULT_DB = Path("/tmp/cause-n-effect-debt-live.sqlite")


def sec_block_message():
    if os.environ.get("SEC_CONTACT_EMAIL", "").strip():
        return None
    return "SEC_CONTACT_EMAIL is missing. No SEC request was sent."


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    db_path = DEFAULT_DB
    if "--db" in args:
        db_path = Path(args[args.index("--db") + 1])
    filings = []
    blocked = sec_block_message()
    if blocked:
        print(blocked)
    else:
        end = date.today().isoformat()
        start = (date.today() - timedelta(days=3)).isoformat()
        print(f"SEC window {start} to {end}, at most 5 in-scope filings")
        filings.extend(fetcher.collect(start, end, limit=5))
    companies = json.loads(UNIVERSE.read_text(encoding="utf-8"))["companies"]
    filings.extend(fetcher.collect_asx(companies))
    if not filings:
        print("ASX watchlist returned no debt headlines.")
        return 1
    if db_path.exists():
        db_path.unlink()
    summary = pipeline.run(filings, db_path)
    print(
        f"run {summary['run_id']} threshold {summary['threshold']:g} "
        f"seen {summary['filings_seen']} cheap {summary['resolved_by_cheap_filter']} "
        f"full {summary['sent_to_full_document']} histogram {summary['score_histogram']}"
    )
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    print("figures:")
    rows = conn.execute(
        """
        SELECT ticker, company, figure_type, instrument, period, value, value_text,
               currency, announced_on, source_url, source_type, verified
        FROM figures
        ORDER BY ticker
        """
    ).fetchall()
    if not rows:
        print("  (none)")
    for row in rows:
        print(
            f"  {row['ticker']} {row['figure_type']} {row['value_text']} "
            f"value={row['value']} {row['currency']} period={row['period']} "
            f"announced_on={row['announced_on']} verified={row['verified']} "
            f"source_type={row['source_type']} url={row['source_url']}"
        )
    print("scores:")
    for row in conn.execute(
        """
        SELECT ticker, accession, form, score, cheap_score, stage_reached, flags
        FROM debt_scout_filing_scores
        ORDER BY ticker
        """
    ):
        print(
            f"  {row['ticker']} score={row['score']} cheap={row['cheap_score']} "
            f"stage={row['stage_reached']} flags={row['flags']} form={row['form']} "
            f"id={row['accession']}"
        )
    conn.close()
    print(f"db {db_path}")
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
