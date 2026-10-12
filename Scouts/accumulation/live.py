"""Live accumulation pull. Writes figures into the shared SQLite history.

    python3 -m Scouts.accumulation.live --db /tmp/cause-n-effect.sqlite
    python3 -m Scouts.accumulation.live --db /tmp/cause-n-effect.sqlite --sec-fixture Scouts/accumulation/fixtures/sec_13d.xml --no-asx

SEC 13D/13G requests run only when SEC_CONTACT_EMAIL is set. ASX Forms 603,
604, and 605 still run. The command exits nonzero when this run stores no figure.
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
from filters import ScreenConfig  # noqa: E402
from model import Document  # noqa: E402
from pipeline import analyse  # noqa: E402
from shared.store import connect, save_run, utc_now  # noqa: E402

DEFAULT_DB = Path("/tmp/cause-n-effect-accumulation.sqlite")
TICKER_FIXTURE = _HERE / "fixtures" / "company_tickers.json"


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
    documents = []
    ticker_map = {}
    fixture = _option(args, "--sec-fixture")
    if fixture:
        fixture = Path(fixture)
        documents.append(
            Document(
                source="sec",
                body=fixture.read_text(encoding="utf-8"),
                url=fixture.as_posix(),
                form_type="SCHEDULE 13D",
                filing_date=date.today().isoformat(),
                accession="0000999001-26-000001",
            )
        )
        ticker_map = fetcher.tickers_from_payload(json.loads(TICKER_FIXTURE.read_text(encoding="utf-8")))
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
            ticker_map = fetcher.fetch_ticker_map()
    if "--no-asx" not in args:
        print(f"ASX 603/604/605, at most {fetcher.ASX_CAP} announcements per company")
        documents.extend(fetcher.collect_asx_markit(fetcher.load_asx()))
    report = analyse(documents, ScreenConfig(as_of=date.today()), ticker_map, {})
    started = utc_now()
    seen = started[:10]
    figures = _unique(_figures(report, seen))
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


def _figures(report, seen):
    rows = []
    for position in report.get("candidates") or []:
        row = _figure(position, seen, verified=1)
        if row:
            rows.append(row)
    for item in report.get("review") or []:
        row = _figure(item.get("position") or {}, seen, verified=0)
        if row:
            rows.append(row)
    return rows


def _figure(position, seen, verified):
    target = position.get("target") or {}
    ticker = (target.get("ticker") or "").upper()
    if not ticker:
        return None
    if position.get("source") == "asx" and not ticker.endswith(".AX"):
        ticker = f"{ticker}.AX"
    stake = position.get("stake_pct")
    shares = position.get("share_count")
    if stake is None and shares is None:
        return None
    filed = (position.get("filing_date") or position.get("event_date") or "")[:10]
    filer = position.get("filer") or ""
    return {
        "ticker": ticker,
        "company": target.get("name") or None,
        "figure_type": "substantial_holding",
        "instrument": position.get("form_type") or "",
        "period": f"{filed}|{position.get('accession') or ''}|{filer}",
        "value": stake if stake is not None else shares,
        "value_text": f"{stake}%" if stake is not None else str(shares),
        "currency": None,
        "seen_at": seen,
        "announced_on": filed or None,
        "source_url": position.get("document_url"),
        "source_type": "asx_announcement" if position.get("source") == "asx" else "filing",
        "verified": verified,
    }


def _unique(figures):
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


def _print_rows(db_path, run_id):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    stored = conn.execute(
        """
        SELECT ticker, figure_type, instrument, period, value, value_text, verified, source_type
        FROM figures WHERE run_id = ? ORDER BY ticker, period
        """,
        (run_id,),
    ).fetchall()
    conn.close()
    for row in stored:
        print(
            f"  {row['ticker']} {row['figure_type']} {row['instrument']} "
            f"period={row['period']} value={row['value']} {row['value_text']} "
            f"verified={row['verified']} source_type={row['source_type']}"
        )
    print(f"db {db_path}")


if __name__ == "__main__":
    sys.exit(main())
