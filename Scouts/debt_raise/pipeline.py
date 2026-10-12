"""Two-stage use-of-proceeds pass, then one analytics row per run."""

import json
import sys
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[1]
for _entry in (str(_REPO), str(_HERE)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from fetcher import (
    fetch_asx_text,
    fetch_phrase_text,
    fetch_primary_text,
    first_page,
    in_scope,
    summary_is_form_label,
)
from score import (
    DEFAULT_MIN_DEAL_USD,
    figure_kind,
    histogram,
    load_config,
    needs_full_document,
    pick_amount,
    score_text,
)
from shared.nethttp import EndpointError, asx_client, sec_client
from store import open_db, save_run, utc_now

# An 8-K and the following 424B for the same notes land a few days apart.
DEAL_REPEAT_DAYS = 7


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
        phrase, cached_body = _phrase_text(filing, client, documents)
        cheap = score_text(phrase, rules)
        flags = []
        body = None
        if needs_full_document(cheap, used):
            stage = "full_document"
            try:
                body = cached_body if cached_body is not None else _full_text(filing, client, documents)
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
            scored_text = phrase
        amount, needs_review = pick_amount(scored_text, min_deal=deal_floor)
        if needs_review:
            flags.append("review")
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
        figure = _figure(filing, scored_text, started_at, amount, deal_floor)
        if figure is not None:
            figures.append(figure)

    figures = _dedupe_issuer_deals(figures)
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


def _phrase_text(filing, client, documents):
    """Return (text for the phrase stage, full body if that fetch already happened).

    SEC index descriptions that are only the form name cannot settle the
    phrase stage. Use a filing-index description or the first page instead.
    """
    summary = filing.get("summary") or ""
    if _is_asx(filing) or not summary_is_form_label(summary, filing.get("form")):
        return summary, None
    accession = filing.get("accession")
    if accession in documents:
        text = documents[accession] or ""
        return first_page(text), text
    try:
        if client is None:
            client = sec_client()
        return fetch_phrase_text(client, filing.get("cik"), accession)
    except EndpointError:
        return summary, None


def _is_asx(filing):
    return (filing.get("form") or "").upper() == "ASX" or filing.get("market") == "ASX"


def _full_text(filing, client, documents):
    accession = filing["accession"]
    if accession in documents:
        return documents[accession]
    asx = _is_asx(filing)
    if client is None:
        client = asx_client() if asx else sec_client()
    if asx:
        return fetch_asx_text(client, filing["document_url"])
    return fetch_primary_text(client, filing["cik"], accession)


def _figure(filing, text, started_at, amount, min_deal=DEFAULT_MIN_DEAL_USD):
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
        "period": _period(filing),
        "value": amount["value"],
        "value_text": amount["text"],
        "currency": amount["currency"],
        "seen_at": started_at[:10],
        "announced_on": filing.get("filed"),
        "source_url": filing.get("document_url") or "",
        "source_type": filing.get("source_type") or "filing",
        "verified": 1,
        "form": filing.get("form") or "",
        "accession": filing.get("accession") or "",
    }


def _dedupe_issuer_deals(figures, window_days=DEAL_REPEAT_DAYS):
    """Keep one filing when the same issuer repeats the same amount within a few days.

    The later filing is kept. An 8-K and a 424B for the same notes are one deal.
    A different amount is a different deal and stays.
    """
    kept = []
    for figure in figures:
        match = next((prior for prior in kept if _same_deal(prior, figure, window_days)), None)
        if match is None:
            kept.append(figure)
            continue
        if _on(figure) >= _on(match):
            kept.remove(match)
            kept.append(figure)
            _log_repeat(match, figure)
        else:
            _log_repeat(figure, match)
    return kept


def _same_deal(left, right, window_days):
    if (left.get("ticker") or "") != (right.get("ticker") or ""):
        return False
    if (left.get("currency") or "") != (right.get("currency") or ""):
        return False
    if (left.get("figure_type") or "") != (right.get("figure_type") or ""):
        return False
    if round(float(left.get("value") or 0), 2) != round(float(right.get("value") or 0), 2):
        return False
    left_on = _on(left)
    right_on = _on(right)
    if left_on is None or right_on is None:
        return False
    return abs((left_on - right_on).days) <= int(window_days)


def _on(figure):
    raw = (figure.get("announced_on") or "")[:10]
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


def _log_repeat(dropped, kept):
    print(
        "dropped duplicate deal"
        f" ticker={dropped.get('ticker') or ''}"
        f" form={dropped.get('form') or ''}"
        f" accession={dropped.get('accession') or ''}"
        f" filed={dropped.get('announced_on') or ''}"
        f" value={float(dropped.get('value') or 0):.0f}"
        f" kept_form={kept.get('form') or ''}"
        f" kept_accession={kept.get('accession') or ''}"
        f" kept_filed={kept.get('announced_on') or ''}",
        file=sys.stderr,
    )


def _period(filing):
    """Same-day filings share a filed date. The accession keeps the figures key unique."""
    filed = filing.get("filed") or ""
    accession = filing.get("accession") or ""
    return f"{filed} {accession}".strip()
