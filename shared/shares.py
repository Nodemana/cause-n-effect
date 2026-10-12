"""Shares outstanding from SEC companyfacts.

The figure type is `dei:EntityCommonStockSharesOutstanding`. A filer that
omits that tag falls back to `CommonStockSharesOutstanding`. The helper
stores the share count only. An official price source is TODO: this module
does not request or store a price, and it does not call an unofficial feed.
Shares without that price leave market cap unverified.

`cache_shares_outstanding` does not open a socket. The caller passes a
client (the pipeline CLI passes the SEC client when `SEC_CONTACT_EMAIL`
is set). A missing client returns without a request.
"""

import logging
import re

from shared.store import save_run

log = logging.getLogger(__name__)

SHARES_FIGURE_TYPE = "dei:EntityCommonStockSharesOutstanding"
SHARES_TYPES = (
    SHARES_FIGURE_TYPE,
    "EntityCommonStockSharesOutstanding",
    "CommonStockSharesOutstanding",
)
COMPANYFACTS = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
_CIK = re.compile(r"/data/(\d+)/")
_TAG_ORDER = (
    ("dei", "EntityCommonStockSharesOutstanding", SHARES_FIGURE_TYPE),
    ("us-gaap", "CommonStockSharesOutstanding", "CommonStockSharesOutstanding"),
)


def companyfacts_url(cik):
    return COMPANYFACTS.format(cik=str(int(cik)).zfill(10))


def shares_from_companyfacts(payload):
    """Latest positive share count. No price.

    `dei:EntityCommonStockSharesOutstanding` wins when it has a positive
    fact. Otherwise the us-gaap common-stock tag is used.
    """
    facts = (payload or {}).get("facts") or {}
    for taxonomy, tag, figure_type in _TAG_ORDER:
        chosen = _latest_shares((facts.get(taxonomy) or {}).get(tag) or {})
        if chosen is None:
            continue
        chosen["figure_type"] = figure_type
        chosen["tag"] = tag
        return chosen
    return None


def companies_from_figures(figures):
    """Unique `{ticker, cik, company}` rows whose source URL contains a CIK."""
    found = {}
    for figure in figures or []:
        ticker = _field(figure, "ticker")
        if not ticker or ticker in found:
            continue
        match = _CIK.search(_field(figure, "source_url") or "")
        if not match:
            continue
        found[ticker] = {
            "ticker": ticker,
            "cik": str(int(match.group(1))),
            "company": _field(figure, "company") or ticker,
        }
    return list(found.values())


def cache_shares_outstanding(conn, companies, *, client=None, fetched_at):
    """Store one shares row per ticker that does not already have one.

    `client` must provide `json(url)`. When it is missing this returns an
    empty list and does not open a socket, even if `SEC_CONTACT_EMAIL` is
    set. Nothing here fetches a price.
    """
    if client is None:
        return []
    pending = []
    for company in companies or []:
        ticker = (company.get("ticker") or "").strip()
        cik = company.get("cik")
        if not ticker or not cik or _shares_cached(conn, ticker):
            continue
        pending.append(company)
    if not pending:
        return []
    rows = []
    for company in pending:
        url = companyfacts_url(company["cik"])
        try:
            parsed = shares_from_companyfacts(client.json(url))
        except Exception as exc:
            log.warning(
                "skipped shares ticker=%s cik=%s url=%s: %s",
                company.get("ticker") or "",
                company.get("cik") or "",
                url,
                exc,
            )
            continue
        if not parsed:
            continue
        filed = (parsed.get("filed") or "")[:10]
        rows.append(
            {
                "ticker": company["ticker"],
                "company": company.get("company") or company["ticker"],
                "figure_type": parsed["figure_type"],
                "instrument": "shares",
                "period": parsed.get("end") or filed,
                "value": parsed["value"],
                "value_text": _plain(parsed["value"]),
                "currency": None,
                "seen_at": (fetched_at or "")[:10] or fetched_at,
                "announced_on": filed or None,
                "source_url": url,
                "source_type": "filing",
                "verified": 1,
            }
        )
    if not rows:
        return []
    save_run(
        conn,
        "shares",
        fetched_at,
        rows,
        notes="companyfacts shares outstanding; official price source TODO; no unofficial price feed",
    )
    return rows


def _latest_shares(node):
    best = None
    for unit, facts in (node.get("units") or {}).items():
        if not _share_unit(unit):
            continue
        for fact in facts or []:
            value = fact.get("val")
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue
            if value <= 0:
                continue
            end = fact.get("end") or ""
            filed = fact.get("filed") or ""
            rank = (end, filed)
            if best is None or rank >= best[0]:
                best = (rank, {"value": value, "end": end, "filed": filed})
    if best is None:
        return None
    return best[1]


def _share_unit(unit):
    folded = (unit or "").casefold()
    return "share" in folded or folded == "pure"


def _shares_cached(conn, ticker):
    placeholders = ", ".join("?" * len(SHARES_TYPES))
    row = conn.execute(
        f"""
        SELECT 1 FROM figures
        WHERE ticker = ? AND figure_type IN ({placeholders})
          AND value IS NOT NULL AND value > 0 AND verified = 1
        LIMIT 1
        """,
        (ticker, *SHARES_TYPES),
    ).fetchone()
    return row is not None


def _field(figure, name):
    if isinstance(figure, dict):
        return figure.get(name)
    return getattr(figure, name, None)


def _plain(value):
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)
