"""Turn fetched documents into the candidates / review JSON."""

import json

from Scouts.accumulation.fetcher import collect_asx, collect_sec, fetch_ticker_map
from Scouts.accumulation.filters import ScreenConfig, screen
from Scouts.accumulation.parser import parse_document
from shared.store import connect, save_run


def analyse(documents, config=None, ticker_by_cik=None, sectors=None):
    """Parse documents and screen them. `documents` are already in memory."""
    config = config or ScreenConfig()
    ticker_by_cik = ticker_by_cik or {}
    positions = []
    seen = set()
    for document in documents:
        for position in parse_document(document, ticker_by_cik):
            key = (
                position.source,
                position.accession or position.document_url,
                position.form_type,
                position.filer_key(),
                position.target_key(),
            )
            if key in seen:
                continue
            seen.add(key)
            positions.append(position)
    return screen(positions, config, sectors)


def run(start, end, asx_tickers, sectors=None, config=None, sec=None, asx=None):
    """Fetch through nethttp, then analyse. Tests pass fake clients and do not call this."""
    config = config or ScreenConfig()
    documents = collect_sec(start, end, client=sec)
    documents.extend(collect_asx(asx_tickers, client=asx))
    tickers = fetch_ticker_map(sec)
    return analyse(documents, config, tickers, sectors or {})


def to_json(report):
    return json.dumps(report, indent=2) + "\n"


def store_run(db_path, report, started_at, notes=None):
    """Insert one accumulation run. Earlier runs and their figures stay put."""
    seen_at = started_at[:10]
    figures = figures_from_report(report, seen_at)
    conn = connect(db_path)
    try:
        run_id = save_run(conn, "accumulation", started_at, figures, notes=notes)
    finally:
        conn.close()
    return {"run_id": run_id, "figures": len(figures)}


def figures_from_report(report, seen_at):
    rows = []
    for row in report.get("candidates") or []:
        target = row.get("target") or {}
        ticker = target.get("ticker")
        if not ticker:
            continue
        stake = row.get("stake_pct")
        filed = row.get("filing_date") or ""
        rows.append(
            {
                "ticker": ticker,
                "company": target.get("name"),
                "figure_type": "stake_pct",
                "instrument": row.get("filer") or "",
                "period": filed or row.get("event_date") or "",
                "value": stake,
                "value_text": None if stake is None else str(stake),
                "currency": None,
                "seen_at": seen_at,
                "announced_on": filed[:10] or None,
                "source_url": row.get("document_url"),
                "source_type": "asx_announcement" if row.get("source") == "asx" else "filing",
                "verified": 1,
            }
        )
    return rows
