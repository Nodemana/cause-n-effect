"""Live debt-raise pull. Writes figures into the shared SQLite history and exits
nonzero when this run stores none.

    python3 -m Scouts.debt_raise.live --db /tmp/cause-n-effect.sqlite
    python3 -m Scouts.debt_raise.live --db /tmp/cause-n-effect.sqlite --since 2026-10-01 --until 2026-10-08 --tickers SCG,AAPL --limit 50

SEC is contacted only when SEC_CONTACT_EMAIL is set. When it is missing, this
script stops before any SEC request and still runs the ASX watchlist.
`--sec-fixture FILE --no-asx` scores a recorded filings JSON instead of the network.
`--since` and `--until` bound the SEC window. `--tickers` is a comma-separated list.
Tickers are resolved to CIKs and the EDGAR search is filtered with the ciks
parameter. A 424B hit with no ticker in the index is labeled from that CIK.
`--limit` caps in-scope SEC filings (default 50).
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
DEFAULT_LIMIT = 50


def sec_block_message():
    if os.environ.get("SEC_CONTACT_EMAIL", "").strip():
        return None
    return "SEC_CONTACT_EMAIL is missing. No SEC request was sent."


def _option(args, name):
    if name not in args:
        return None
    index = args.index(name)
    if index + 1 >= len(args) or str(args[index + 1]).startswith("--"):
        raise SystemExit(f"{name} needs a value")
    return args[index + 1]


def _tickers(value):
    if not value:
        return None
    rows = [part.strip().upper() for part in value.split(",") if part.strip()]
    return rows or None


def _limit(args):
    raw = _option(args, "--limit")
    if raw is None:
        return DEFAULT_LIMIT
    try:
        value = int(raw)
    except ValueError:
        raise SystemExit("--limit must be an integer") from None
    if value < 1:
        raise SystemExit("--limit must be at least 1")
    return value


def _window(args):
    """SEC date window. `--until` defaults to today; `--since` defaults to three days earlier."""
    until = _option(args, "--until")
    since = _option(args, "--since")
    end = until or date.today().isoformat()
    date.fromisoformat(end)
    if since:
        date.fromisoformat(since)
        return since, end
    start = (date.fromisoformat(end) - timedelta(days=3)).isoformat()
    return start, end


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    db_path = Path(_option(args, "--db") or DEFAULT_DB)
    tickers = _tickers(_option(args, "--tickers"))
    filings = []
    fixture = _option(args, "--sec-fixture")
    if fixture:
        fixture = Path(fixture)
        payload = json.loads(fixture.read_text(encoding="utf-8"))
        rows = payload.get("filings") or []
        if tickers:
            wanted = {(ticker.upper().removesuffix(".AX")) for ticker in tickers}
            rows = [row for row in rows if (row.get("ticker") or "").upper().removesuffix(".AX") in wanted]
        filings.extend(rows)
        print(f"SEC fixture {fixture}")
    else:
        blocked = sec_block_message()
        if blocked:
            print(blocked)
        else:
            start, end = _window(args)
            limit = _limit(args)
            scope = f" for {', '.join(tickers)}" if tickers else ""
            print(f"SEC window {start} to {end}, at most {limit} in-scope filings{scope}")
            found, missing = fetcher.collect_window(start, end, tickers=tickers, limit=limit)
            if missing:
                print(f"unmapped tickers: {', '.join(missing)}")
            filings.extend(found)
    if "--no-asx" not in args:
        companies = json.loads(UNIVERSE.read_text(encoding="utf-8"))["companies"]
        if tickers:
            wanted = {(ticker.upper().removesuffix(".AX")) for ticker in tickers}
            companies = [row for row in companies if (row.get("ticker") or "").upper() in wanted]
        filings.extend(fetcher.collect_asx(companies))
    if not filings:
        print("No filings to score.")
        return 1
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
        WHERE run_id = ?
        ORDER BY ticker
        """,
        (summary["run_id"],),
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
        WHERE run_id = ?
        ORDER BY ticker
        """,
        (summary["run_id"],),
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
