"""Live insider-buying pull. Writes figures into the shared SQLite history.

    python3 -m Scouts.insider_buying.live --db /tmp/cause-n-effect.sqlite

SEC Form 4 requests run only when SEC_CONTACT_EMAIL is set. Otherwise the
script says so and does not send one. ASX Appendix 3Y notices still run.
The command exits nonzero when this run stores no figure.

Offline SEC check, using the fixture XML instead of EDGAR:

    python3 -m Scouts.insider_buying.live --db /tmp/cause-n-effect.sqlite --sec-fixture Scouts/insider_buying/fixtures/sec_form4_ceo.xml --no-asx
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

from Scouts.insider_buying import fetcher  # noqa: E402
from Scouts.insider_buying import scout_pipeline as pipeline  # noqa: E402
from shared.store import connect, save_run, utc_now  # noqa: E402

UNIVERSE = _HERE / "universe_asx.json"
DEFAULT_DB = Path("/tmp/cause-n-effect-insider.sqlite")
DEFAULT_LIMIT = 50


def sec_block_message():
    if os.environ.get("SEC_CONTACT_EMAIL", "").strip():
        return None
    return "SEC_CONTACT_EMAIL is missing. No SEC request was sent."


def _unique_figures(figures):
    """Sum lots that share an insider, ticker, and day.

    A second row used to get a `|2` period suffix so the figures key held.
    Those suffixes made each lot look like an earlier routine print. A row
    that is not one of those lots still gets a suffix when its key repeats.
    """
    grouped = []
    by_lot = {}
    used = set()
    for item in figures:
        key = _lot_key(item)
        if key is not None and key in by_lot:
            current = by_lot[key]
            current["value"] = (current.get("value") or 0) + (item.get("value") or 0)
            count = current.get("_lots", 1) + 1
            current["_lots"] = count
            current["value_text"] = f"{count} lots totalling {current['value']:g}"
            continue
        row = dict(item)
        if key is not None:
            row["period"] = _canonical_period(row.get("period") or "")
            row["_lots"] = 1
            by_lot[key] = row
        period = row.get("period") or ""
        instrument = row.get("instrument") or ""
        store_key = (row.get("ticker"), row.get("figure_type"), instrument, period)
        copy = 2
        while store_key in used:
            period = f"{row.get('period') or ''}|{copy}"
            store_key = (row.get("ticker"), row.get("figure_type"), instrument, period)
            copy += 1
        row["period"] = period
        row["instrument"] = instrument
        used.add(store_key)
        grouped.append(row)
    for row in grouped:
        row.pop("_lots", None)
    return grouped


def _lot_key(item):
    if item.get("figure_type") != "insider_purchase":
        return None
    day, insider = _day_and_insider(item.get("period") or "")
    if not day or not insider:
        return None
    return (
        (item.get("ticker") or "").upper(),
        item.get("figure_type"),
        item.get("instrument") or "",
        (item.get("currency") or "").upper(),
        day,
        insider,
    )


def _canonical_period(period):
    day, _insider = _day_and_insider(period)
    if not day:
        return period
    _day, _, rest = (period or "").partition("|")
    parts = rest.split("|")
    if len(parts) >= 2 and parts[-1].isdigit():
        rest = "|".join(parts[:-1])
    return f"{day}|{rest}"


def _day_and_insider(period):
    text = period or ""
    if "|" not in text:
        return "", ""
    day, _, rest = text.partition("|")
    if len(day) != 10 or day[4] != "-" or day[7] != "-":
        return "", ""
    parts = rest.split("|")
    if len(parts) >= 2 and parts[-1].isdigit():
        insider = "|".join(parts[:-1])
    else:
        insider = rest
    insider = insider.strip().casefold()
    if not insider:
        return "", ""
    return day, insider


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python3 -m Scouts.insider_buying.live",
        description="Store Form 4 code P purchases and ASX Appendix 3Y on-market buys.",
    )
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite history path")
    parser.add_argument("--sec-fixture", help="read this Form 4 XML instead of contacting EDGAR")
    parser.add_argument("--no-asx", action="store_true", help="do not request ASX announcements")
    parser.add_argument(
        "--limit",
        type=_positive_int,
        default=DEFAULT_LIMIT,
        help=f"maximum SEC Form 4 filings to download (default {DEFAULT_LIMIT})",
    )
    return parser


def _positive_int(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("--limit must be an integer") from None
    if number < 1:
        raise argparse.ArgumentTypeError("--limit must be at least 1")
    return number


def main(argv=None):
    args = build_parser().parse_args(argv)
    db_path = Path(args.db)
    documents = []
    if args.sec_fixture:
        fixture = Path(args.sec_fixture)
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
            print(f"SEC Form 4 window {start} to {end}, at most {args.limit} filings")
            documents.extend(fetcher.collect_sec(start, end, limit=args.limit))
    if not args.no_asx:
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
    notes = f"records={len(table['records'])} review={len(table['review'])}"
    conn = connect(db_path)
    try:
        run_id = save_run(conn, "insider-buying", started, figures, notes=notes)
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
