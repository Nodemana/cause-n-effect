"""Refresh the macro cache. Does not lodge leads.

    python3 -m Scouts.macro.live --help
    python3 -m Scouts.macro.live --db /tmp/cause-n-effect.sqlite
    python3 -m Scouts.macro.live --db /tmp/cause-n-effect.sqlite --force
    python3 -m Scouts.macro.live --db /tmp/cause-n-effect.sqlite --fixture-dir Scouts/macro/fixtures

FRED series are skipped when FRED_API_KEY is unset. EIA series are skipped
when EIA_API_KEY is unset. Neither key is printed or stored.
`--help` parses arguments and exits before any request.
"""

import argparse
import sys
from pathlib import Path

from .pipeline import refresh, risk_note_line
from .store import open_db

DEFAULT_DB = Path("/tmp/cause-n-effect-macro.sqlite")


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Refresh the macro cache Centurions read. "
            "Writes macro_series and macro_observations. Does not lodge leads."
        )
    )
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB),
        help="SQLite path (default: %(default)s)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Refresh every selected series even when the cache is still fresh",
    )
    parser.add_argument(
        "--series",
        help="Comma-separated series ids (default: the whole catalog)",
    )
    parser.add_argument(
        "--fixture-dir",
        help="Read fixture files instead of the network",
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    series_ids = None
    if args.series is not None:
        series_ids = [part.strip() for part in args.series.split(",")]
    try:
        conn = open_db(args.db)
    except OSError as exc:
        print(f"database: {exc}")
        return 1
    try:
        try:
            results = refresh(
                conn,
                series_ids=series_ids,
                force=args.force,
                fixture_dir=args.fixture_dir,
            )
        except ValueError as exc:
            print(str(exc))
            return 2
    finally:
        conn.close()
    for row in results:
        status = row["status"]
        if status == "refreshed":
            print(f"{row['series_id']} refreshed {row['count']}")
        elif status == "fresh":
            print(f"{row['series_id']} fresh")
        elif status == "skipped":
            print(f"{row['series_id']} skipped {row['reason']}")
        else:
            print(f"{row['series_id']} error {row['reason']}")
    print(risk_note_line())
    print(f"db {args.db}")
    if any(row["status"] == "error" for row in results):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
