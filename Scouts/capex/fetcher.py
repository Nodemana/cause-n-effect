"""SEC companyfacts, EDGAR full-text guidance hits, and ASX announcement lists.

Pass a client in tests. With no client, SEC calls use sec_client and ASX calls
use asx_client. Both come from shared/nethttp.py. sec_client refuses to send
when SEC_CONTACT_EMAIL is missing. asx_client spaces requests by 2 seconds.
"""

import re
import sys
import urllib.parse
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
    """Latest reported facts for each capex tag. `client` is required in tests."""
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
    those are equal, the latest filing wins. Cash-flow tags stay as the span
    SEC reported. Quarter values are derived later.
    """
    cutoff = period_cutoff(quarters, as_of)
    gaap = (payload.get("facts") or {}).get("us-gaap") or {}
    chosen = []
    for tag in CAPEX_TAGS:
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
            for row in ordered[:n]:
                chosen.append({"tag": tag, "unit": unit, "row": row})
    return chosen


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
