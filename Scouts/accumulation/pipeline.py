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
    """One figure per screened position, including review rows that parsed.

    verified is 1 only when the cover page of the primary document yielded both
    the percent of class and the share count. Currency is USD for SEC and AUD
    for ASX. The source URL is the primary document, and the accession is kept
    on that URL and in the period.
    """
    rows = []
    for row in report.get("candidates") or []:
        built = figure_from_position(row, seen_at, [])
        if built:
            rows.append(built)
    for item in report.get("review") or []:
        built = figure_from_position(item.get("position") or {}, seen_at, item.get("flags") or [])
        if built:
            rows.append(built)
    return rows


def figure_from_position(position, seen_at, flags):
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
    accession = position.get("accession") or ""
    if stake is not None and shares is not None:
        text = f"{stake}% ({shares} shares)"
    elif stake is not None:
        text = f"{stake}%"
    else:
        text = str(shares)
    return {
        "ticker": ticker,
        "company": target.get("name") or None,
        "figure_type": "substantial_holding",
        "instrument": position.get("form_type") or "",
        "period": f"{filed}|{accession}|{filer}",
        "value": stake if stake is not None else shares,
        "value_text": text,
        "currency": _figure_currency(position),
        "seen_at": seen_at,
        "announced_on": filed or None,
        "source_url": _source_url(position.get("document_url"), accession),
        "source_type": "asx_announcement" if position.get("source") == "asx" else "filing",
        "verified": _cover_verified(stake, shares, flags),
    }


def _figure_currency(position):
    """A percent is not a dollar amount. Share counts are not a currency either."""
    if position.get("source") != "asx":
        return "USD"
    if position.get("stake_pct") is not None:
        return "pct"
    return None


def _cover_verified(stake, shares, flags):
    """1 when percent of class and shares were both read from the cover page."""
    if "unparseable_stake" in (flags or []):
        return 0
    if stake is None or shares is None:
        return 0
    return 1


def _source_url(url, accession):
    text = (url or "").strip()
    if not text:
        return None
    acc = (accession or "").strip()
    compact = acc.replace("-", "")
    if acc and acc not in text and (not compact or compact not in text):
        sep = "&" if "?" in text else "?"
        return f"{text}{sep}accession={acc}"
    return text
