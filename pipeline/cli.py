"""Command line for the lead pipeline.

`python -m pipeline run --help`, `status --help`, `ingest --help`, and
`rejects --help` parse arguments and exit. They do not open a database
or a socket.

Judgement is an external agent. The commands do not call a model and do
not read an API key. See docs/agent-protocol.md.
"""

import argparse
import sys

from shared.pipeline_store import connect

from pipeline.agent import AgentResultError
from pipeline.flow import ingest_run, pipeline_status, run_pipeline
from pipeline.store import search_rejects
from shared.nethttp import sec_client, sec_contact_email


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        return _run(parser, args)
    if args.command == "status":
        return _status(parser, args)
    if args.command == "ingest":
        return _ingest(parser, args)
    if args.command == "rejects":
        return _rejects(parser, args)
    parser.error("choose run, status, ingest, or rejects")
    return 2


def build_parser():
    parser = argparse.ArgumentParser(
        prog="pipeline",
        description=(
            "Sweep the latest scout figures into pending decisions. "
            "Deterministic gates run in process. Significance, Centurion theses, "
            "and Praetorian reviews are task files under data/agent_queue. "
            "A run records status awaiting_agents and exits. "
            "ingest reads result files and advances until ranking writes pending decisions."
        ),
    )
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser(
        "run",
        help="Filter figures and queue significance tasks for an external agent",
        description=(
            "Read each scout's current figures from its latest run. "
            "A run recorded with a ticker scope replaces only those tickers; "
            "earlier runs stay in the database as comparison history. "
            "Identical figures collapse to the newest row. "
            "An insider trade more than 30 days before its filing date is dropped. "
            "Thresholds and dedupe run first. Each figure that still needs a "
            "significance judgement is written to "
            "data/agent_queue/<run_id>/significance/figure-<figure_id>.task.json. "
            "The database status is awaiting_agents. The process exits 0 and "
            "does not call a model. "
            "Shares outstanding are cached from SEC companyfacts when "
            "SEC_CONTACT_EMAIL is set. Counterparty names are resolved on ingest, "
            "after significance, from 8-K Item 1.01 agreements, EX-10 exhibits, "
            "and backlog 10-K concentration notes. "
            "Each name is classified from the sentence as a customer, a supplier, "
            "or a deal party, so a customer named in a capex filing stays a customer. "
            "Affiliates, subsidiaries, and former parents are dropped. Only a "
            "matched SEC registrant or a company-suffix name is kept, as a whole word. "
            "A source error drops that company's parties and the contact address is not logged. "
            "A market cap is shares outstanding times an official share-price figure. "
            "The figures summary is the lead figures used after --limit. "
            "Ranking later writes at most --max-theses pending decisions and "
            "data/reports/<run_id>.md beside the queue directory. "
            "A Praetorian critique lists every flaw in each thesis and does not drop a lead."
        ),
    )
    run.add_argument("--db", required=True, help="SQLite database that holds scout figures")
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute the sweep and print the summary without writing leads, rejects, or decisions",
    )
    run.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Use only the first N lead figures, newest announced_on first. The figures summary is that count",
    )
    run.add_argument(
        "--queue-dir",
        default=None,
        help="Directory for agent task files (default data/agent_queue)",
    )
    run.add_argument(
        "--macro-cache",
        default=None,
        help="JSON file with a summary key. Missing file skips macro context on a capex chain and on a later merge",
    )
    run.add_argument(
        "--max-theses",
        type=int,
        default=5,
        help=(
            "Pending decisions and data/reports/<run_id>.md include at most this many theses. "
            "Default 5. With the default queue data/agent_queue the report is data/reports/<run_id>.md"
        ),
    )

    status = sub.add_parser(
        "status",
        help="List agent task files that do not yet have a result",
        description=(
            "Print the run status, the stage, and each outstanding "
            "<lead_id>.task.json. A finished run has status complete and stage ranking."
        ),
    )
    status.add_argument("--db", required=True, help="SQLite database written by pipeline run")
    status.add_argument("--run", required=True, help="pipeline run id")

    ingest = sub.add_parser(
        "ingest",
        help="Validate result files and advance to the next stage",
        description=(
            "Read <lead_id>.result.json beside each task in the current stage. "
            "A missing file or a value that breaks the output schema is an error "
            "and the stage does not advance. Valid results are stored. "
            "The next stage's task files are written. "
            "Order: significance, then one Centurion task per lead. "
            "Linkable capex, orders backlog, and debt raises are one capex-chain lead. "
            "Same-ticker insider and accumulation signals inside the window are one smart-money lead. "
            "A macro lead stays separate, and its Centurion task lists related findings from those scouts. "
            "A leftover cross-group merge, when one exists, gets a second Centurion pass. "
            "A Praetorian critique then lists every flaw. It does not reject or drop a lead. "
            "Ranking and pending decisions follow. The flaw list is shown in the report and does not remove a lead from ranking. "
            "Ranking writes at most the run's --max-theses theses to data/reports/<run_id>.md. "
            "Merge and ranking do not write task files. "
            "When SEC_CONTACT_EMAIL is set, the significance ingest names counterparties "
            "before the Centurion tasks are written. The address is not logged."
        ),
    )
    ingest.add_argument("--db", required=True, help="SQLite database written by pipeline run")
    ingest.add_argument("--run", required=True, help="pipeline run id")
    ingest.add_argument(
        "--queue-dir",
        default=None,
        help="Must match the directory used by pipeline run, when passed",
    )

    rejects = sub.add_parser(
        "rejects",
        help="Search the reject log by issuer ticker or counterparty",
        description=(
            "Print lead_rejects rows. --ticker matches the issuer. "
            "--counterparty matches a buyer, supplier, customer, or holder by "
            "case-insensitive token prefix (north matches Northwind) or ticker prefix (amz matches AMZN). "
            "Pass either flag, or both to require a match on each."
        ),
    )
    rejects.add_argument("--db", required=True, help="SQLite database written by pipeline run")
    rejects.add_argument("--ticker", help="Issuer ticker, for example AMZN")
    rejects.add_argument("--counterparty", help="Counterparty name or ticker, for example a supplier")
    return parser


def _run(parser, args):
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be a positive integer")
    if args.max_theses < 1:
        parser.error("--max-theses must be a positive integer")
    shares_client = sec_client() if sec_contact_email() else None
    result = run_pipeline(
        args.db,
        dry_run=args.dry_run,
        limit=args.limit,
        macro_cache=args.macro_cache,
        shares_client=shares_client,
        queue_dir=args.queue_dir,
        max_theses=args.max_theses,
    )
    _print_result(result)
    return 0


def _status(parser, args):
    try:
        report = pipeline_status(args.db, args.run)
    except AgentResultError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"run_id: {report['run_id']}")
    print(f"status: {report['status']}")
    print(f"stage: {report['stage']}")
    print(f"queue_dir: {report['queue_dir']}")
    print(f"outstanding: {len(report['outstanding'])}")
    for item in report["outstanding"]:
        print(f"{item['lead_id']} {item['stage']} {item['task']}")
    return 0


def _ingest(parser, args):
    parties_client = sec_client() if sec_contact_email() else None
    try:
        result = ingest_run(
            args.db,
            args.run,
            queue_dir=args.queue_dir,
            parties_client=parties_client,
        )
    except AgentResultError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    _print_result(result)
    return 0


def _print_result(result):
    for key in (
        "run_id",
        "status",
        "stage",
        "tasks",
        "queue_dir",
        "figures",
        "leads",
        "rejects",
        "merges",
        "rankings",
        "decisions",
        "dry_run",
        "macro",
        "report",
    ):
        print(f"{key}: {result[key]}")


def _rejects(parser, args):
    if not args.ticker and not args.counterparty:
        parser.error("pass --ticker or --counterparty")
    conn = connect(args.db)
    try:
        found = search_rejects(conn, ticker=args.ticker, counterparty=args.counterparty)
    finally:
        conn.close()
    if not found:
        print("no rejects")
        return 0
    for row, parties in found:
        print(_format_reject(row, parties))
    return 0


def _format_reject(row, parties):
    labels = " ".join(
        f"{name}={row[column] or '-'}"
        for name, column in (
            ("magnitude", "significance_magnitude"),
            ("novelty", "significance_novelty"),
            ("attention", "significance_attention"),
            ("timing", "significance_timing"),
        )
    )
    rendered = []
    for party in parties:
        ticker = party["ticker"]
        suffix = f" ({ticker})" if ticker else ""
        rendered.append(f"{party['role']}:{party['name']}{suffix}")
    return (
        f"ticker={row['ticker']} cik={row['cik'] or ''} "
        f"filing_date={row['filing_date'] or ''} scout={row['scout']} "
        f"stage={row['rejection_stage']} reason={row['reason']} {labels} "
        f"url={row['source_url'] or ''} accession={row['accession'] or ''} "
        f"counterparties={', '.join(rendered) if rendered else '-'}"
    )
