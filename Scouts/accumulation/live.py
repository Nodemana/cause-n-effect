"""Live accumulation pull. Writes figures into the shared SQLite history.

    python3 -m Scouts.accumulation.live --db /tmp/cause-n-effect.sqlite
    python3 -m Scouts.accumulation.live --help
    python3 -m Scouts.accumulation.live --db /tmp/cause-n-effect.sqlite --sec-fixture Scouts/accumulation/fixtures/sec_13d.xml --no-asx

SEC 13D/13G requests run only when SEC_CONTACT_EMAIL is set. ASX Forms 603,
604, and 605 still run. The command exits nonzero when this run stores no figure.
A figure is verified when the primary document's cover page yielded both the
percent of class and the share count. The source URL is that document, and the
accession is stored with it.
"""

import argparse
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
from filters import ScreenConfig  # noqa: E402
from model import Document  # noqa: E402
from pipeline import analyse, figures_from_report  # noqa: E402
from shared.store import connect, save_run, utc_now  # noqa: E402

DEFAULT_DB = Path("/tmp/cause-n-effect-accumulation.sqlite")
TICKER_FIXTURE = _HERE / "fixtures" / "company_tickers.json"
FIXTURE_ACCESSION = "0000999001-26-000001"
FIXTURE_URL = "https://www.sec.gov/Archives/edgar/data/999001/000099900126000001/primary_doc.xml"


def sec_block_message():
    if os.environ.get("SEC_CONTACT_EMAIL", "").strip():
        return None
    return "SEC_CONTACT_EMAIL is missing. No SEC request was sent."


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python3 -m Scouts.accumulation.live",
        description="Store 13D/13G and ASX 603/604/605 stakes in the shared figures table.",
    )
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite history path")
    parser.add_argument("--sec-fixture", help="read this filing instead of contacting EDGAR")
    parser.add_argument("--no-asx", action="store_true", help="do not request ASX announcements")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    db_path = Path(args.db)
    documents = []
    ticker_map = {}
    watchlist = fetcher.load_watchlist()
    if args.sec_fixture:
        fixture = Path(args.sec_fixture)
        documents.append(
            Document(
                source="sec",
                body=fixture.read_text(encoding="utf-8"),
                url=FIXTURE_URL,
                form_type="SCHEDULE 13D",
                filing_date=date.today().isoformat(),
                accession=FIXTURE_ACCESSION,
            )
        )
        ticker_map = fetcher.tickers_from_payload(
            json.loads(TICKER_FIXTURE.read_text(encoding="utf-8")),
            watchlist,
        )
        print(f"SEC fixture {fixture}")
    else:
        blocked = sec_block_message()
        if blocked:
            print(blocked)
        else:
            end = date.today().isoformat()
            start = (date.today() - timedelta(days=7)).isoformat()
            print(f"SEC 13D/13G window {start} to {end}, at most 5 filings")
            documents.extend(fetcher.collect_sec(start, end, limit=5))
            ticker_map = fetcher.fetch_ticker_map(watchlist=watchlist)
    if not args.no_asx:
        print(f"ASX 603/604/605, at most {fetcher.ASX_CAP} announcements per company")
        documents.extend(fetcher.collect_asx_markit(fetcher.load_asx()))
    report = analyse(documents, ScreenConfig(as_of=date.today()), ticker_map, {})
    started = utc_now()
    seen = started[:10]
    figures = figures_from_report(report, seen)
    if not figures:
        print("No accumulation figures.")
        return 1
    notes = f"candidates={len(report['candidates'])} review={len(report['review'])}"
    conn = connect(db_path)
    try:
        run_id = save_run(conn, "accumulation", started, figures, notes=notes)
    finally:
        conn.close()
    print(f"run {run_id} figures {len(figures)} review {len(report['review'])}")
    _print_rows(db_path, run_id)
    return 0


def _print_rows(db_path, run_id):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    stored = conn.execute(
        """
        SELECT ticker, figure_type, instrument, period, value, value_text, currency,
               source_url, verified, source_type
        FROM figures WHERE run_id = ? ORDER BY ticker, period
        """,
        (run_id,),
    ).fetchall()
    conn.close()
    for row in stored:
        print(
            f"  {row['ticker']} {row['figure_type']} {row['instrument']} "
            f"period={row['period']} value={row['value']} {row['value_text']} "
            f"currency={row['currency']} verified={row['verified']} "
            f"source_type={row['source_type']} url={row['source_url']}"
        )
    print(f"db {db_path}")


if __name__ == "__main__":
    sys.exit(main())
