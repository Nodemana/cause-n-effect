"""Fetch backlog and order documents through shared.nethttp.

Pass a client in tests. With no client, SEC calls use sec_client and ASX
calls use asx_client. IR calls use a nethttp Client and only the URLs listed
on the supplier.
"""

import json
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from shared.nethttp import Client, EndpointError, asx_client, sec_client

HERE = Path(__file__).resolve().parent
CUSTOMERS_PATH = HERE / "universe_customers.json"
SUPPLIERS_PATH = HERE / "universe_suppliers.json"
ASX_PATH = HERE / "universe_asx.json"
ASX_API = "https://asx.api.markitdigital.com/asx-research/1.0"
ASX_CAP = 5

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


def collect_supplier_reports(companies, client=None):
    """Latest 10-Q or 10-K narrative for each supplier.

    Eaton states firm backlog in the 10-Q, not in an OrdersReceived fact and not
    in the earnings bullet list. The primary HTML is parsed as a release.
    """
    client = client or sec_client()
    documents = []
    for company in companies:
        cik = company.get("cik")
        if not cik:
            continue
        url = submissions_url(cik)
        _require_sec(url)
        payload = client.json(url)
        filing = latest_periodic(payload)
        if not filing:
            continue
        base = _archive_base(cik, filing["accession"])
        index_url = base + "/index.json"
        _require_sec(index_url)
        index = client.json(index_url)
        name = _primary_html(index, filing.get("primary_document"))
        if not name:
            continue
        doc_url = base + "/" + name
        _require_sec(doc_url)
        body, _ctype = client.get(doc_url, accept="text/html,text/plain")
        documents.append(
            {
                "company": company,
                "body": _decode(body),
                "url": doc_url,
                "filing_date": filing["filing_date"],
                "form": filing["form"],
                "accession": filing["accession"],
            }
        )
    return documents


def latest_periodic(payload):
    """The newest 10-Q or 10-K in a submissions document. 10-Q wins a tie on the date."""
    recent = ((payload or {}).get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    accessions = recent.get("accessionNumber") or []
    dates = recent.get("filingDate") or []
    best = None
    for index, form in enumerate(forms):
        if form not in {"10-Q", "10-K"}:
            continue
        filed = dates[index] if index < len(dates) else ""
        primary_docs = recent.get("primaryDocument") or []
        row = {
            "form": form,
            "accession": accessions[index] if index < len(accessions) else "",
            "filing_date": filed,
            "primary_document": primary_docs[index] if index < len(primary_docs) else "",
        }
        if not row["accession"] or not filed:
            continue
        if best is None or (filed, form == "10-Q") > (best["filing_date"], best["form"] == "10-Q"):
            best = row
    return best


def _primary_html(index, preferred=None):
    """The 10-Q or 10-K narrative, not the last R*.htm report in the index.

    EDGAR lists rendered XBRL reports (R1.htm, R9.htm, ...) after the primary
    document. The last HTML file is one of those reports. Eaton's segment
    backlog is in the primary document; R9.htm only repeats the total.
    """
    names = []
    directory = (index or {}).get("directory") or {}
    items = directory.get("item") or []
    if isinstance(items, dict):
        items = [items]
    for item in items:
        name = item.get("name") or ""
        if name.lower().endswith((".htm", ".html")):
            names.append(name)
    if preferred:
        for name in names:
            if name.lower() == str(preferred).lower():
                return name
    narrative = [name for name in names if _is_narrative_html(name)]
    if narrative:
        return narrative[0]
    return None


def _is_narrative_html(name):
    lower = (name or "").lower()
    if not lower.endswith((".htm", ".html")):
        return False
    if "index" in lower or _is_report_html(lower) or _is_exhibit(lower):
        return False
    return True


def _is_report_html(name):
    stem = name.rsplit(".", 1)[0]
    return len(stem) > 1 and stem[0] == "r" and stem[1:].isdigit()


def _is_exhibit(name):
    lower = (name or "").lower().rsplit("/", 1)[-1]
    if "exhibit" in lower:
        return True
    for index in range(len(lower) - 2):
        if lower.startswith("ex", index) and lower[index + 2].isdigit():
            return True
    return False


def collect_8k_exhibits(companies, client=None, items=_8K_SUPPLIER_ITEMS, since=None, limit_per_company=None):
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
        filings = _recent_8k(payload, items)
        if since:
            filings = [row for row in filings if (row.get("filing_date") or "") >= since]
        if limit_per_company is not None:
            filings = filings[: int(limit_per_company)]
        for filing in filings:
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
                        "accession": filing.get("accession") or "",
                    }
                )
    return documents


def load_asx(path=ASX_PATH):
    return _load(path, "companies")


def collect_asx(companies, client=None, count=ASX_CAP):
    """Latest ASX order and backlog headlines. The feed returns at most five per company."""
    client = client or asx_client()
    count = min(int(count), ASX_CAP)
    documents = []
    for company in companies:
        code = (company.get("ticker") or "").strip().upper()
        if not code:
            continue
        url = f"{ASX_API}/companies/{code.lower()}/announcements?count={count}"
        _require_asx(url)
        payload = client.json(url)
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or "items" not in data:
            raise EndpointError(f"ASX announcements feed for {code} failed the health check")
        name = data.get("displayName") or company.get("name") or code
        company = {**company, "name": name, "ticker": code, "market": "ASX"}
        for item in (data.get("items") or [])[:count]:
            headline = (item.get("headline") or "").strip()
            key = item.get("documentKey") or ""
            if not headline or not key or not _supplier_headline(headline):
                continue
            doc_url = f"{ASX_API}/file/{key}"
            _require_asx(doc_url)
            body, _ctype = client.get(doc_url, accept="application/pdf,text/html,text/plain")
            documents.append(
                {
                    "company": company,
                    "body": _asx_text(body),
                    "url": doc_url,
                    "filing_date": str(item.get("date") or "")[:10] or None,
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
    return any(word in text for word in ("order", "orders", "contract", "backlog", "turbine", "transformer", "switchgear"))


def _require_sec(url):
    _require(url, "sec.gov")


def _require_asx(url):
    host = (urllib.parse.urlsplit(url).hostname or "").lower().rstrip(".")
    official = host == "asx.com.au" or host.endswith(".asx.com.au") or host == "asx.api.markitdigital.com"
    if not official:
        raise EndpointError(f"refusing non-official host for {url}")


def _asx_text(body):
    if isinstance(body, str):
        body = body.encode("utf-8")
    if body.startswith(b"%PDF"):
        return _pdf_text(body)
    return _decode(body)


def _pdf_text(body):
    with tempfile.TemporaryDirectory() as tmp:
        pdf_path = Path(tmp) / "announcement.pdf"
        txt_path = Path(tmp) / "announcement.txt"
        pdf_path.write_bytes(body)
        try:
            completed = subprocess.run(
                ["pdftotext", "-layout", str(pdf_path), str(txt_path)],
                check=False,
                capture_output=True,
            )
        except FileNotFoundError as exc:
            raise EndpointError("pdftotext is not installed") from exc
        if completed.returncode != 0 or not txt_path.exists():
            raise EndpointError("pdftotext failed on ASX announcement")
        return txt_path.read_text(encoding="utf-8", errors="replace")


def _require(url, suffix):
    host = (urllib.parse.urlsplit(url).hostname or "").lower().rstrip(".")
    if host != suffix and not host.endswith("." + suffix):
        raise EndpointError(f"refusing non-official host for {url}")


def _decode(body):
    if isinstance(body, str):
        return body
    return body.decode("utf-8", errors="replace")
