"""Live capex pull. Writes a gitignored SQLite file and prints the figures.

The owner runs this on their machine. It is not part of the fixture tests.

SEC is contacted only when SEC_CONTACT_EMAIL is set. When it is missing, this
script stops before any SEC request. ASX still runs when universe_asx.json
lists companies. The ASX client waits 2 seconds between requests and asks for
at most five announcements per company.

    python3 -m Scouts.capex.live --db /tmp/cause-n-effect.sqlite
    python3 -m Scouts.capex.live --db /tmp/cause-n-effect.sqlite --sec-fixture Scouts/capex/fixtures/companyfacts_amzn.json --no-asx

The default database is /tmp/cause-n-effect-capex.sqlite. SEC names are
AMZN, ORCL, and MSFT in universe_sec.json. Guidance search covers the last
90 days. Cash-flow tags keep the reported span. A single quarter is derived when the
prior year-to-date figure is present. Periods older than --quarters (default 8)
are dropped.
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

from Scouts.capex import fetcher  # noqa: E402
from Scouts.capex import scout_pipeline as pipeline  # noqa: E402
from shared.store import utc_now  # noqa: E402

SEC_UNIVERSE = _HERE / "universe_sec.json"
ASX_UNIVERSE = _HERE / "universe_asx.json"
DEFAULT_DB = Path("/tmp/cause-n-effect-capex.sqlite")
GUIDANCE_DAYS = 90


def sec_block_message():
    if os.environ.get("SEC_CONTACT_EMAIL", "").strip():
        return None
    return "SEC_CONTACT_EMAIL is missing. No SEC request was sent."


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python3 -m Scouts.capex.live",
        description="Store SEC companyfacts, capex guidance, and ASX capex headlines.",
    )
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite history path")
    parser.add_argument("--sec-fixture", help="read this companyfacts JSON instead of contacting EDGAR")
    parser.add_argument("--no-asx", action="store_true", help="do not request ASX announcements")
    parser.add_argument(
        "--quarters",
        type=int,
        default=fetcher.RECENT_QUARTERS,
        help=f"keep companyfacts whose period end falls in the last N quarters (default {fetcher.RECENT_QUARTERS})",
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    db_path = Path(args.db)
    quarters = args.quarters
    sec_companies = json.loads(SEC_UNIVERSE.read_text(encoding="utf-8"))["companies"]
    asx_companies = [] if args.no_asx else json.loads(ASX_UNIVERSE.read_text(encoding="utf-8"))["companies"]
    facts = []
    guidance = []
    if args.sec_fixture:
        fixture = Path(args.sec_fixture)
        payload = json.loads(fixture.read_text(encoding="utf-8"))
        company = sec_companies[0]
        facts = [
            {
                "ticker": company["ticker"],
                "cik": str(int(company["cik"])).zfill(10),
                "company": payload.get("entityName") or company.get("name"),
                "facts": fetcher.select_facts(payload, ticker=company["ticker"], quarters=quarters),
            }
        ]
        print(f"SEC fixture {fixture}")
    else:
        blocked = sec_block_message()
        if blocked:
            print(blocked)
        else:
            end = date.today().isoformat()
            start = (date.today() - timedelta(days=GUIDANCE_DAYS)).isoformat()
            names = ", ".join(company["ticker"] for company in sec_companies)
            print(f"SEC companyfacts for {names}")
            print(f"EDGAR full-text capex guidance {start} to {end}")
            facts = fetcher.collect_companyfacts(sec_companies, quarters=quarters)
            guidance = fetcher.collect_guidance(sec_companies, start=start, end=end)
    announcements = []
    if args.no_asx:
        pass
    elif asx_companies:
        print(f"ASX announcements, at most {fetcher.ASX_CAP} per company")
        announcements = fetcher.collect_asx(asx_companies)
    else:
        print("ASX watchlist is empty. No ASX request was sent.")
    if not facts and not guidance and not announcements:
        print("No capex figures.")
        return 1
    started = utc_now()
    seen = started[:10]
    notes = (
        "SEC companyfacts and EDGAR full-text guidance; "
        f"ASX list capped at {fetcher.ASX_CAP}. "
        f"Cash-flow tags are the reported span; quarters={quarters}."
    )
    summary = pipeline.run(
        db_path,
        facts,
        guidance,
        announcements,
        started_at=started,
        seen_at=seen,
        notes=notes,
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
            f"  {row['ticker']} {row['figure_type']} {row['instrument']} period={row['period']} "
            f"value={row['value']} {row['currency'] or ''} text={row['value_text']} "
            f"announced_on={row['announced_on']} verified={row['verified']} "
            f"source_type={row['source_type']} url={row['source_url']}"
        )
    conn.close()
    print(f"db {db_path}")
    return 0 if rows else 1


if __name__ == "__main__":
    sys.exit(main())
