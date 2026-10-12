"""Compare threshold settings against full-document reads across runs."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[1]
for _entry in (str(_REPO), str(_HERE)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from score import BUCKETS, load_config

RUNS_SQL = """
SELECT r.run_id, r.started_at, d.threshold, d.filings_seen,
       d.resolved_by_cheap_filter, d.sent_to_full_document, d.score_histogram
FROM debt_scout_runs d
JOIN runs r ON r.run_id = d.run_id
ORDER BY r.started_at, r.run_id
"""

MID_SQL = """
SELECT d.threshold AS threshold, s.score AS score, COUNT(*) AS filings
FROM debt_scout_filing_scores s
JOIN debt_scout_runs d ON d.run_id = s.run_id
WHERE s.stage_reached = 'cheap_filter'
  AND s.score >= ?
  AND s.score <= ?
GROUP BY d.threshold, s.score
ORDER BY d.threshold, s.score
"""


def render(db_path, config=None):
    loaded = config or load_config()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        runs = conn.execute(RUNS_SQL).fetchall()
        mid = conn.execute(MID_SQL, (loaded["mid_min"], loaded["mid_max"])).fetchall()
    finally:
        conn.close()
    lines = [
        "debt-raise threshold report",
        "A lower threshold sends more filings to the full-document pass (more prospectus reads).",
        "A higher threshold leaves mid-range scores on the cheap filter (those raises are not read in full).",
        f"Mid band from scoring.json: {loaded['mid_min']:g} to {loaded['mid_max']:g}.",
        "",
    ]
    if not runs:
        lines.append("No debt-raise runs.")
        return "\n".join(lines) + "\n"
    header = (
        f"{'run':<32} {'started_at':<22} {'threshold':>9} {'seen':>6} "
        f"{'cheap':>6} {'full':>6} {'full_%':>7}  histogram"
    )
    lines.append(header)
    for row in runs:
        seen = row["filings_seen"] or 0
        full = row["sent_to_full_document"] or 0
        ratio = (100.0 * full / seen) if seen else 0.0
        lines.append(
            f"{row['run_id']:<32} {row['started_at']:<22} {row['threshold']:>9g} {seen:>6} "
            f"{row['resolved_by_cheap_filter']:>6} {full:>6} {ratio:>6.1f}%  "
            f"{_format_histogram(row['score_histogram'])}"
        )
    lines.append("")
    lines.append("Cheap-filter scores inside the mid band (threshold left them unread):")
    if not mid:
        lines.append("(none)")
    else:
        lines.append(f"{'threshold':>9} {'score':>7} {'filings':>8}")
        for row in mid:
            lines.append(f"{row['threshold']:>9g} {row['score']:>7g} {row['filings']:>8}")
    return "\n".join(lines) + "\n"


def _format_histogram(raw):
    counts = json.loads(raw)
    return " ".join(f"{bucket}={counts.get(bucket, 0)}" for bucket in BUCKETS)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Compare debt-raise thresholds and full-document reads")
    parser.add_argument("--db", required=True, help="SQLite history database")
    args = parser.parse_args(argv)
    sys.stdout.write(render(args.db))


if __name__ == "__main__":
    main()
