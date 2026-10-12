"""SEC companyfacts, EDGAR full-text guidance hits, and ASX announcement lists.

Pass a client in tests. With no client, SEC calls use sec_client and ASX calls
use asx_client. Both come from shared/nethttp.py. sec_client refuses to send
when SEC_CONTACT_EMAIL is missing. asx_client spaces requests by 2 seconds.
"""

import re
import sys
import urllib.parse
from collections import Counter
from datetime import date
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[1]
for _entry in (str(_REPO), str(_HERE)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from shared.nethttp import EndpointError, asx_client, sec_client  # noqa: E402
from shared.store import log_duplicate, prefer_fact  # noqa: E402

COMPANYFACTS = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
ASX_API = "https://asx.api.markitdigital.com/asx-research/1.0"
ASX_CAP = 5

# Reported capex cash-flow tags, plus PP&E and finance-lease balances.
CAPEX_TAGS = (
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "PaymentsToAcquireProductiveAssets",
    "PaymentsForCapitalImprovements",
    "PaymentsToAcquireOtherPropertyPlantAndEquipment",
    "PropertyPlantAndEquipmentNet",
    "PropertyPlantAndEquipmentGross",
    "FinanceLeaseRightOfUseAsset",
    "FinanceLeaseLiability",
    "FinanceLeaseLiabilityNoncurrent",
    "FinanceLeaseLiabilityCurrent",
)
STATEMENT_FORMS = frozenset({"10-K", "10-Q", "20-F", "10-K/A", "10-Q/A", "20-F/A"})
RECENT_QUARTERS = 8
# Year-over-year needs the current quarter and the one four quarters earlier.
MIN_SINGLE_QUARTERS = 5
FLOW_TAGS = frozenset(
    {
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
        "PaymentsForCapitalImprovements",
        "PaymentsToAcquireOtherPropertyPlantAndEquipment",
    }
)
GUIDANCE_QUERY = '"capital expenditure" OR "capital expenditures" OR capex'
GUIDANCE_FORMS = "8-K,8-K/A,10-K,10-K/A,10-Q,10-Q/A"
_ASX_CAPEX = re.compile(
    r"capex|capital expenditure|capital investment|property,\s*plant|\bppe\b",
    re.I,
)


def collect_companyfacts(companies, client=None, n=4, quarters=RECENT_QUARTERS, as_of=None):
    """Reported facts for each capex tag. `client` is required in tests.

    `n` caps balance-sheet tags. Cash-flow tags keep the quarter window instead,
    so a year-to-date row cannot push out the year-ago quarter.
    """
    client = client or sec_client()
    found = []
    for company in companies:
        cik = _cik(company.get("cik"))
        url = companyfacts_url(cik)
        _require_sec(url)
        payload = client.json(url)
        found.append(
            {
                "ticker": company["ticker"],
                "cik": cik,
                "company": payload.get("entityName") or company.get("name") or company["ticker"],
                "facts": select_facts(
                    payload, n, quarters=quarters, as_of=as_of, ticker=company["ticker"]
                ),
            }
        )
    return found


def companyfacts_url(cik):
    return COMPANYFACTS.format(cik=_cik(cik))


def period_cutoff(quarters=RECENT_QUARTERS, as_of=None):
    """First day of the window covering the last `quarters` quarters."""
    as_of = as_of or date.today()
    month = as_of.month - 3 * int(quarters)
    year = as_of.year
    while month <= 0:
        month += 12
        year -= 1
    return date(year, month, 1).isoformat()


def select_facts(payload, n=4, quarters=RECENT_QUARTERS, as_of=None, ticker=""):
    """One row per tag, unit, and period. Non-statements and periods before the window are dropped.

    A single-quarter value beats a year-to-date value for that same key. When
    those are equal, the latest filing wins. Each duration is labelled quarter,
    ytd, ttm, or annual. Year-to-date is only a span that starts on the
    fiscal-year start and is not itself a quarter or a full year.

    Cash-flow tags keep every span in the window (at least five quarters).
    `n` used to cap those spans together, so the latest year-to-date and
    trailing-twelve-month rows filled a `quarters=8` pull and the year-ago
    quarter was never stored. `n` still caps balance-sheet tags.
    """
    flow_cutoff = period_cutoff(max(int(quarters), MIN_SINGLE_QUARTERS), as_of)
    other_cutoff = period_cutoff(quarters, as_of)
    gaap = (payload.get("facts") or {}).get("us-gaap") or {}
    fiscal_start = infer_fiscal_start(payload)
    chosen = []
    for tag in CAPEX_TAGS:
        cutoff = flow_cutoff if tag in FLOW_TAGS else other_cutoff
        node = gaap.get(tag) or {}
        for unit, rows in (node.get("units") or {}).items():
            if not _monetary(unit):
                continue
            best = {}
            for row in rows:
                if row.get("form") not in STATEMENT_FORMS:
                    continue
                end = row.get("end")
                if not end or end < cutoff:
                    continue
                key = (row.get("start"), end)
                previous = best.get(key)
                if previous is None or prefer_fact(row, previous):
                    if previous is not None:
                        log_duplicate(
                            _fact_view(tag, previous, ticker, unit),
                            _fact_view(tag, row, ticker, unit),
                        )
                    best[key] = row
                else:
                    log_duplicate(
                        _fact_view(tag, row, ticker, unit),
                        _fact_view(tag, previous, ticker, unit),
                    )
            ordered = sorted(
                best.values(),
                key=lambda row: (row.get("end") or "", row.get("start") or ""),
                reverse=True,
            )
            # Balance-sheet snapshots stay on `n`. Cash-flow spans do not share
            # that cap: the quarter window is what limits them.
            picked = ordered if tag in FLOW_TAGS else ordered[:n]
            for row in picked:
                chosen.append(
                    {
                        "tag": tag,
                        "unit": unit,
                        "row": row,
                        "period_label": span_label(row.get("start"), row.get("end"), fiscal_start),
                    }
                )
    return chosen


def infer_fiscal_start(payload):
    """Month-day the fiscal year starts, from Q1 and full-year statement spans.

    Calendar year (01-01) is the fallback when the filing does not say.
    """
    gaap = (payload.get("facts") or {}).get("us-gaap") or {}
    votes = []
    for node in gaap.values():
        if not isinstance(node, dict):
            continue
        for unit, rows in (node.get("units") or {}).items():
            if not _monetary(unit):
                continue
            for row in rows or []:
                if row.get("form") not in STATEMENT_FORMS:
                    continue
                start = row.get("start") or ""
                end = row.get("end") or ""
                if len(start) < 10:
                    continue
                fp = (row.get("fp") or "").upper()
                if fp == "Q1":
                    votes.append(start[5:10])
                elif fp == "FY" and end and _span_days(start, end) >= 350:
                    votes.append(start[5:10])
    if not votes:
        return "01-01"
    return Counter(votes).most_common(1)[0][0]


def span_label(start, end, fiscal_start="01-01"):
    """quarter, ytd, ttm, or annual. Instant facts (no start) have no span label.

    ytd is only a span that begins on the fiscal-year start and is longer than
    a quarter and shorter than a year. A twelve-month span that begins then is
    annual. The same length beginning on any other day is ttm.
    """
    if not start or not end:
        return ""
    days = _span_days(start, end)
    if days is None:
        return ""
    on_year_start = start[5:10] == fiscal_start
    if 80 <= days <= 115:
        return "quarter"
    if 350 <= days <= 380:
        return "annual" if on_year_start else "ttm"
    if on_year_start:
        return "ytd"
    return ""


def _span_days(start, end):
    try:
        return (date.fromisoformat(end) - date.fromisoformat(start)).days
    except ValueError:
        return None


def collect_guidance(companies, client=None, start=None, end=None, limit=20):
    """EDGAR full-text hits for capex-guidance phrases, limited per company."""
    client = client or sec_client()
    found = []
    for company in companies:
        cik = _cik(company.get("cik"))
        url = efts_url(cik, start, end)
        _require_sec(url)
        page = client.json(url)
        hits = ((page.get("hits") or {}).get("hits")) or []
        for hit in hits[:limit]:
            found.append(
                {
                    "ticker": company["ticker"],
                    "cik": cik,
                    "company": company.get("name") or company["ticker"],
                    "hit": hit,
                }
            )
    return found


def efts_url(cik, start, end, offset=0):
    query = urllib.parse.urlencode(
        {
            "q": GUIDANCE_QUERY,
            "dateRange": "custom",
            "startdt": start,
            "enddt": end,
            "forms": GUIDANCE_FORMS,
            "ciks": _cik(cik),
            "from": str(offset),
        }
    )
    return f"{EFTS_URL}?{query}"


def collect_asx(companies, client=None, count=ASX_CAP):
    """Latest ASX announcements, capped at five per company. The feed shape is checked."""
    client = client or asx_client()
    count = min(int(count), ASX_CAP)
    rows = []
    for company in companies:
        code = (company.get("ticker") or "").strip().upper()
        if not code:
            continue
        url = f"{ASX_API}/companies/{code.lower()}/announcements?count={count}"
        _require_asx(url)
        payload = client.json(url)
        check_feed(payload, code)
        data = payload.get("data") or {}
        name = data.get("displayName") or company.get("name") or code
        for item in (data.get("items") or [])[:count]:
            headline = (item.get("headline") or "").strip()
            key = item.get("documentKey") or ""
            if not headline or not key or not _ASX_CAPEX.search(headline):
                continue
            filed = str(item.get("date") or "")[:10]
            rows.append(
                {
                    "ticker": f"{code}.AX",
                    "company": name,
                    "headline": headline,
                    "filed": filed or None,
                    "document_key": key,
                    "source_url": f"{ASX_API}/file/{key}",
                }
            )
    return rows


def check_feed(payload, code):
    """The announcements API is undocumented. A response without data.items is unhealthy."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or "items" not in data:
        raise EndpointError(f"ASX announcements feed for {code} failed the health check")


def _fact_view(tag, row, ticker, unit):
    start = row.get("start")
    end = row.get("end") or ""
    return {
        "ticker": ticker,
        "figure_type": tag,
        "instrument": "",
        "period": f"{start}/{end}" if start else end,
        "value": row.get("val"),
        "currency": unit,
        "filed": row.get("filed"),
        "frame": row.get("frame"),
        "fp": row.get("fp"),
        "start": start,
        "end": row.get("end"),
    }


def _cik(value):
    return str(int(str(value))).zfill(10)


def _monetary(unit):
    token = (unit or "").upper()
    return len(token) == 3 and token.isalpha()


def _require_sec(url):
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    if host != "sec.gov" and not host.endswith(".sec.gov"):
        raise EndpointError(f"refusing non-SEC host for {url}")


def _require_asx(url):
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    official = host == "asx.com.au" or host.endswith(".asx.com.au") or host == "asx.api.markitdigital.com"
    if not official:
        raise EndpointError(f"refusing non-ASX host for {url}")
