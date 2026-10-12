"""Turn fetched documents into the candidates / review JSON."""

import json

from scouts.accumulation.fetcher import collect_asx, collect_sec, fetch_ticker_map
from scouts.accumulation.filters import ScreenConfig, screen
from scouts.accumulation.parser import parse_document


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
