"""Command line for the lead pipeline.

`python -m pipeline run --help` and `python -m pipeline rejects --help`
parse arguments and exit. They do not open a database or a socket.

LLM_API_KEY, LLM_BASE_URL, and LLM_MODEL configure the live client.
When LLM_API_KEY is unset, the sweep uses the offline stub provider.
The key is never printed.
"""

import argparse

from shared.pipeline_store import connect

from pipeline.flow import run_pipeline
from pipeline.load import FIGURE_WINDOW_DAYS
from pipeline.store import search_rejects
from shared.nethttp import sec_client, sec_contact_email

DEFAULT_TOKEN_BUDGET = 80000


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        return _run(parser, args)
    if args.command == "rejects":
        return _rejects(parser, args)
    parser.error("choose run or rejects")
    return 2


def build_parser():
    parser = argparse.ArgumentParser(
        prog="pipeline",
        description=(
            "Sweep the latest scout figures into pending decisions. "
            "The live model is an OpenAI-compatible HTTP client. "
            "With no LLM_API_KEY the offline stub runs the same stages and does not open a network connection."
        ),
    )
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser(
        "run",
        help="Filter, research, merge, critique, rank, and record pending decisions",
        description=(
            f"Read each scout's figures from the last {FIGURE_WINDOW_DAYS} days, "
            "the latest row per ticker, figure type, instrument, and period. "
            "Thresholds and dedupe run first. A per-run token budget then rates "
            "magnitude, novelty, market attention, and timing. Surviving leads "
            "each get one Centurion thesis from Centurions/rules/<type>.md. "
            "The Legate merges linked theses, and only those merged leads get a "
            "second Centurion pass (price run new, acceleration, or priced-in, "
            "plus macro text when a cache file is present). Praetorians critique "
            "the final theses. The Legate ranks them and writes pending decisions "
            "for the shortlist. The second pass classifies a prior price run as new, "
            "acceleration, or priced-in and notes that call as a concern. Praetorians "
            "agree or disagree, with counter-evidence. Market attention is stored as "
            "significance_attention. Macro context is read from macro_series and "
            "macro_observations when those tables have rows, and otherwise from "
            "cache/macro.json or --macro-cache. "
            "LLM_API_KEY, LLM_BASE_URL, and LLM_MODEL configure the live client. "
            "When LLM_API_KEY is unset the offline stub runs the same stages and "
            "does not open a network connection. The key is never printed. "
            "Shares outstanding are cached from SEC companyfacts when "
            "SEC_CONTACT_EMAIL is set. An official price source is TODO; "
            "the sweep does not call an unofficial price feed. "
            "The figures summary is the lead figures used after --limit."
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
        "--token-budget",
        type=int,
        default=DEFAULT_TOKEN_BUDGET,
        help=f"Maximum tokens for this run (default {DEFAULT_TOKEN_BUDGET})",
    )
    run.add_argument(
        "--macro-cache",
        default=None,
        help="JSON file with a summary key. Missing file skips macro context",
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
    if args.token_budget < 0:
        parser.error("--token-budget must be zero or positive")
    shares_client = sec_client() if sec_contact_email() else None
    result = run_pipeline(
        args.db,
        dry_run=args.dry_run,
        limit=args.limit,
        token_budget=args.token_budget,
        macro_cache=args.macro_cache,
        shares_client=shares_client,
    )
    for key in (
        "run_id",
        "provider",
        "tokens_used",
        "token_budget",
        "figures",
        "leads",
        "rejects",
        "merges",
        "rankings",
        "decisions",
        "dry_run",
        "macro",
    ):
        print(f"{key}: {result[key]}")
    return 0


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
