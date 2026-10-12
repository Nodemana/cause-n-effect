"""Fetch backlog and order documents through shared.nethttp.

Pass a client in tests. With no client, SEC calls use sec_client and ASX
calls use asx_client. IR calls use a nethttp Client and only the URLs listed
on the supplier.
"""

import json
import sys
import urllib.parse
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from shared.nethttp import Client, EndpointError, asx_client, sec_client

HERE = Path(__file__).resolve().parent
CUSTOMERS_PATH = HERE / "universe_customers.json"
SUPPLIERS_PATH = HERE / "universe_suppliers.json"

_8K_CUSTOMER_ITEMS = ("2.02", "9.01")
_8K_SUPPLIER_ITEMS = ("7.01", "8.01", "9.01")


def load_customers(path=CUSTOMERS_PATH):
    return _load(path, "companies")


def load_suppliers(path=SUPPLIERS_PATH):
    return _load(path, "companies")


def company_facts_url(cik):
    return f"https://data.sec.gov/api/xbrl/companyfacts/CIK{_cik10(cik)}.json"


def submissions_url(cik):
    return f"https://data.sec.gov/submissions/CIK{_cik10(cik)}.json"


def collect_customer_facts(companies, client=None):
    client = client or sec_client()
    found = []
    for company in companies:
        cik = company.get("cik")
        if not cik:
            continue
        url = company_facts_url(cik)
        _require_sec(url)
        found.append((company, client.json(url), url))
    return found


def collect_supplier_facts(companies, client=None):
    return collect_customer_facts(companies, client)


def collect_8k_exhibits(companies, client=None, items=_8K_SUPPLIER_ITEMS):
    """Download 8-K exhibit text for filings whose items overlap `items`."""
    client = client or sec_client()
    documents = []
    for company in companies:
        cik = company.get("cik")
        if not cik:
            continue
        url = submissions_url(cik)
        _require_sec(url)
        payload = client.json(url)
        for filing in _recent_8k(payload, items):
            base = _archive_base(cik, filing["accession"])
            index_url = base + "/index.json"
            _require_sec(index_url)
            index = client.json(index_url)
            for name in _exhibit_names(index):
                doc_url = base + "/" + name
                _require_sec(doc_url)
                body, _ctype = client.get(doc_url, accept="text/html,text/plain")
                documents.append(
                    {
                        "company": company,
                        "body": _decode(body),
                        "url": doc_url,
                        "filing_date": filing["filing_date"],
                        "form": "8-K",
                    }
                )
    return documents


def collect_asx(companies, client=None, count=40):
    client = client or asx_client()
    documents = []
    for company in companies:
        if (company.get("market") or "").upper() != "ASX":
            continue
        url = "https://www.asx.com.au/asx/1/company/{}/announcements?{}".format(
            urllib.parse.quote(company["ticker"]),
            urllib.parse.urlencode({"count": str(count)}),
        )
        _require_asx(url)
        payload = client.json(url)
        for item in _announcements(payload):
            if not _supplier_headline(item["headline"]):
                continue
            _require_asx(item["url"])
            body, _ctype = client.get(item["url"], accept="text/html,text/plain")
            documents.append(
                {
                    "company": company,
                    "body": _decode(body),
                    "url": item["url"],
                    "filing_date": (item.get("date") or "")[:10] or None,
                    "form": "ASX",
                }
            )
    return documents


def collect_ir(companies, client=None):
    client = client or Client("cause-n-effect (company releases)", min_interval=1.0)
    documents = []
    allowed = {}
    for company in companies:
        for url in company.get("ir_urls") or []:
            allowed[url] = company
    for url, company in allowed.items():
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        if host.endswith("sec.gov") or host.endswith("asx.com.au"):
            raise EndpointError("IR list must not repeat SEC or ASX hosts; those have their own collectors")
        body, _ctype = client.get(url, accept="text/html,text/plain")
        documents.append(
            {
                "company": company,
                "body": _decode(body),
                "url": url,
                "filing_date": None,
                "form": "IR",
            }
        )
    return documents


def _load(path, key):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = payload.get(key) or []
    if not isinstance(rows, list):
        raise ValueError(f"{path} {key} must be a list")
    return rows


def _cik10(cik):
    digits = "".join(ch for ch in str(cik) if ch.isdigit())
    return digits.zfill(10)


def _recent_8k(payload, items):
    recent = ((payload.get("filings") or {}).get("recent")) or {}
    forms = recent.get("form") or []
    accessions = recent.get("accessionNumber") or []
    dates = recent.get("filingDate") or []
    item_rows = recent.get("items") or []
    wanted = set(items)
    found = []
    for index, form in enumerate(forms):
        if form != "8-K":
            continue
        filed_items = set(_split_items(item_rows[index] if index < len(item_rows) else ""))
        if wanted and not (filed_items & wanted):
            continue
        found.append(
            {
                "accession": accessions[index],
                "filing_date": dates[index] if index < len(dates) else None,
            }
        )
    return found


def _split_items(value):
    return [part.strip() for part in str(value).replace(";", ",").split(",") if part.strip()]


def _archive_base(cik, accession):
    cik_path = str(int(_cik10(cik).lstrip("0") or "0"))
    return f"https://www.sec.gov/Archives/edgar/data/{cik_path}/{accession.replace('-', '')}"


def _exhibit_names(index):
    directory = (index or {}).get("directory") or {}
    items = directory.get("item") or []
    if isinstance(items, dict):
        items = [items]
    names = []
    for item in items:
        name = item.get("name") or ""
        lower = name.lower()
        if not lower.endswith((".htm", ".html", ".txt")):
            continue
        if "99" in lower or "ex99" in lower or "exhibit99" in lower:
            names.append(name)
    return names


def _announcements(payload):
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict):
        rows = data.get("items") or []
    elif isinstance(data, list):
        rows = data
    else:
        rows = []
    for row in rows:
        url = row.get("url") or ""
        if not url:
            continue
        yield {
            "headline": row.get("headline") or row.get("header") or "",
            "url": url,
            "date": row.get("date") or row.get("document_release_date") or row.get("document_date"),
        }


def _supplier_headline(headline):
    text = (headline or "").casefold()
    return any(word in text for word in ("order", "contract", "turbine", "transformer", "switchgear"))


def _require_sec(url):
    _require(url, "sec.gov")


def _require_asx(url):
    _require(url, "asx.com.au")


def _require(url, suffix):
    host = (urllib.parse.urlsplit(url).hostname or "").lower().rstrip(".")
    if host != suffix and not host.endswith("." + suffix):
        raise EndpointError(f"refusing non-official host for {url}")


def _decode(body):
    if isinstance(body, str):
        return body
    return body.decode("utf-8", errors="replace")
