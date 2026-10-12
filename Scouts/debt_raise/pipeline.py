"""Two-stage use-of-proceeds pass, then one analytics row per run."""

import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[1]
for _entry in (str(_REPO), str(_HERE)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from fetcher import fetch_asx_text, fetch_primary_text, in_scope
from score import (
    DEFAULT_MIN_DEAL_USD,
    deal_amount,
    figure_kind,
    histogram,
    load_config,
    needs_full_document,
    score_text,
)
from shared.nethttp import EndpointError, asx_client, sec_client
from store import open_db, save_run, utc_now


def run(
    filings,
    db_path,
    threshold=None,
    client=None,
    documents=None,
    config=None,
    started_at=None,
    min_deal=None,
):
    """Score index text, read a full filing only when the cheap filter says to.

    `documents` maps an accession to fixture text so tests never open a socket.
    `client` is used for the full-document pass when the accession is not in
    `documents`. The cheap pass does not fetch.
    """
    loaded = config or load_config()
    rules = loaded["rules"]
    used = loaded["threshold"] if threshold is None else float(threshold)
    deal_floor = float(loaded.get("min_deal_usd", DEFAULT_MIN_DEAL_USD) if min_deal is None else min_deal)
    started_at = started_at or utc_now()
    documents = documents or {}

    scores = []
    figures = []
    for filing in filings:
        if not in_scope(filing.get("form"), filing.get("items")):
            continue
        cheap = score_text(filing.get("summary") or "", rules)
        flags = []
        body = None
        if needs_full_document(cheap, used):
            stage = "full_document"
            try:
                body = _full_text(filing, client, documents)
            except EndpointError:
                body = None
            if not body:
                flags.append("unreadable")
                final = cheap
                scored_text = filing.get("summary") or ""
            else:
                final = score_text(body, rules)
                scored_text = body
                if not final.decided:
                    flags.append("undecided")
        else:
            stage = "cheap_filter"
            final = cheap
            scored_text = filing.get("summary") or ""
        row = {
            "accession": filing["accession"],
            "ticker": filing.get("ticker"),
            "score": final.score,
            "stage_reached": stage,
            "form": filing.get("form"),
            "cheap_score": cheap.score,
            "decided": 1 if final.decided else 0,
            "flags": json.dumps(flags),
        }
        scores.append(row)
        figure = _figure(filing, scored_text, started_at, deal_floor)
        if figure is not None:
            figures.append(figure)

    scores.sort(key=lambda row: row["accession"])
    buckets = histogram(row["score"] for row in scores)
    cheap_n = sum(1 for row in scores if row["stage_reached"] == "cheap_filter")
    full_n = sum(1 for row in scores if row["stage_reached"] == "full_document")
    conn = open_db(db_path)
    try:
        run_id = save_run(
            conn,
            started_at,
            used,
            len(scores),
            cheap_n,
            full_n,
            buckets,
            scores,
            figures,
            notes=f"threshold={used}",
        )
    finally:
        conn.close()
    return {
        "run_id": run_id,
        "started_at": started_at,
        "threshold": used,
        "filings_seen": len(scores),
        "resolved_by_cheap_filter": cheap_n,
        "sent_to_full_document": full_n,
        "score_histogram": buckets,
    }


def _full_text(filing, client, documents):
    accession = filing["accession"]
    if accession in documents:
        return documents[accession]
    asx = (filing.get("form") or "").upper() == "ASX" or filing.get("market") == "ASX"
    if client is None:
        client = asx_client() if asx else sec_client()
    if asx:
        return fetch_asx_text(client, filing["document_url"])
    return fetch_primary_text(client, filing["cik"], accession)


def _figure(filing, text, started_at, min_deal=DEFAULT_MIN_DEAL_USD):
    amount = deal_amount(text, min_deal=min_deal)
    ticker = filing.get("ticker") or ""
    if amount is None or not ticker:
        return None
    if amount["currency"] == "USD" and amount["value"] < float(min_deal):
        form = filing.get("form") or ""
        print(
            "dropped deal below minimum"
            f" ticker={ticker}"
            f" form={form}"
            f" accession={filing.get('accession') or ''}"
            f" value={amount['value']:.0f}"
            f" {amount['currency']}"
            f" minimum={float(min_deal):.0f}",
            file=sys.stderr,
        )
        return None
    instrument, figure_type = figure_kind(text)
    return {
        "ticker": ticker,
        "company": filing.get("company") or ticker,
        "figure_type": figure_type,
        "instrument": instrument,
        "period": filing.get("filed") or "",
        "value": amount["value"],
        "value_text": amount["text"],
        "currency": amount["currency"],
        "seen_at": started_at[:10],
        "announced_on": filing.get("filed"),
        "source_url": filing.get("document_url") or "",
        "source_type": filing.get("source_type") or "filing",
        "verified": 1,
    }
