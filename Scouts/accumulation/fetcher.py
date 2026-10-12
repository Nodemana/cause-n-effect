"""Fetch Schedule 13D/13G and ASX substantial holder notices through nethttp.

Nothing in this module opens a socket itself. Pass a client to tests. When
client is omitted, the call uses shared.nethttp.sec_client or asx_client.
"""

import json
import re
import subprocess
import tempfile
import urllib.parse
from pathlib import Path

from Scouts.accumulation.forms import SEC_FORM_TYPES
from Scouts.accumulation.model import Document
from shared.nethttp import EndpointError, asx_client, sec_client

EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
ASX_ANN_URL = "https://www.asx.com.au/asx/1/company/{ticker}/announcements"
ASX_API = "https://asx.api.markitdigital.com/asx-research/1.0"
ASX_CAP = 5
_UNIVERSE = Path(__file__).resolve().parent / "universe_asx.json"
_EFTS_PAGE = 100


def collect_sec(start, end, client=None, form_types=SEC_FORM_TYPES, limit=None):
    """Search EDGAR for the date window and download each primary document."""
    client = client or sec_client()
    documents = []
    for hit in iter_sec_hits(client, start, end, form_types):
        source = hit.get("_source") or {}
        accession = source.get("adsh") or ""
        ciks = source.get("ciks") or []
        if not accession or not ciks:
            continue
        form_type = source.get("form") or ""
        filing_date = _iso_prefix(source.get("file_date") or source.get("display_date_filed"))
        url, body = fetch_primary_doc(client, ciks[0], accession)
        documents.append(
            Document(
                source="sec",
                body=body,
                url=url,
                form_type=form_type,
                filing_date=filing_date,
                accession=accession,
            )
        )
        if limit is not None and len(documents) >= int(limit):
            break
    return documents


def iter_sec_hits(client, start, end, form_types=SEC_FORM_TYPES):
    offset = 0
    while True:
        url = efts_url(start, end, form_types, offset)
        _require_sec(url)
        page = client.json(url)
        hits = ((page.get("hits") or {}).get("hits")) or []
        for hit in hits:
            yield hit
        total = ((page.get("hits") or {}).get("total") or {}).get("value", 0)
        offset += len(hits)
        if not hits or offset >= total:
            return


def efts_url(start, end, form_types, offset=0):
    """A `q=*` term makes this EDGAR search return no hits, so the query is forms and dates only."""
    query = urllib.parse.urlencode(
        {
            "dateRange": "custom",
            "startdt": start,
            "enddt": end,
            "forms": ",".join(form_types),
            "from": str(offset),
        }
    )
    return f"{EFTS_URL}?{query}"


def fetch_primary_doc(client, cik, accession):
    base = archive_base(cik, accession)
    index_url = base + "/index.json"
    _require_sec(index_url)
    index = client.json(index_url)
    name = choose_primary_name(_index_names(index))
    if not name:
        raise EndpointError(f"no primary_doc.xml in {index_url}")
    url = base + "/" + name
    _require_sec(url)
    body, _content_type = client.get(url, accept="application/xml,text/html,text/plain")
    return url, _decode(body)


def fetch_ticker_map(client=None):
    """Map a 10-digit issuer CIK to a ticker using the SEC company ticker file."""
    client = client or sec_client()
    _require_sec(TICKERS_URL)
    return tickers_from_payload(client.json(TICKERS_URL))


def tickers_from_payload(payload):
    mapping = {}
    for row in (payload or {}).values():
        try:
            cik = str(int(row["cik_str"])).zfill(10)
        except (KeyError, TypeError, ValueError):
            continue
        ticker = (row.get("ticker") or "").upper()
        if ticker:
            mapping[cik] = ticker
    return mapping


def load_asx(path=None):
    payload = json.loads(Path(path or _UNIVERSE).read_text(encoding="utf-8"))
    rows = payload.get("companies") or []
    if not isinstance(rows, list):
        raise ValueError("universe_asx.json companies must be a list")
    return rows


def collect_asx(tickers, client=None, count=50):
    """Download substantial holder notices for each ASX code."""
    client = client or asx_client()
    documents = []
    for ticker in tickers:
        url = asx_announcements_url(ticker, count)
        _require_asx(url)
        payload = client.json(url)
        for item in iter_announcements(payload):
            form = asx_form_from_headline(item["headline"])
            if not form:
                continue
            doc_url = item["url"]
            _require_asx(doc_url)
            body, _content_type = client.get(doc_url, accept="text/html,text/plain,application/pdf")
            documents.append(
                Document(
                    source="asx",
                    body=_decode(body),
                    url=doc_url,
                    form_type=form,
                    filing_date=_iso_prefix(item.get("date")),
                    ticker=ticker.upper(),
                )
            )
    return documents


def collect_asx_markit(companies, client=None, count=ASX_CAP):
    """Latest 603/604/605 notices from the Markit research feed. The feed returns at most five."""
    client = client or asx_client()
    count = min(int(count), ASX_CAP)
    documents = []
    for company in companies:
        code = (company.get("ticker") if isinstance(company, dict) else company or "").strip().upper()
        if not code:
            continue
        url = f"{ASX_API}/companies/{code.lower()}/announcements?count={count}"
        _require_markit(url)
        payload = client.json(url)
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or "items" not in data:
            raise EndpointError(f"ASX announcements feed for {code} failed the health check")
        for item in (data.get("items") or [])[:count]:
            headline = (item.get("headline") or "").strip()
            form = asx_form_from_headline(headline)
            key = item.get("documentKey") or ""
            if not form or not key:
                continue
            doc_url = f"{ASX_API}/file/{key}"
            _require_markit(doc_url)
            body, _content_type = client.get(doc_url, accept="application/pdf,text/html,text/plain")
            documents.append(
                Document(
                    source="asx",
                    body=_asx_text(body),
                    url=doc_url,
                    form_type=form,
                    filing_date=_iso_prefix(item.get("date")),
                    ticker=code,
                )
            )
    return documents


def asx_announcements_url(ticker, count=50):
    code = urllib.parse.quote(ticker.strip().upper())
    return ASX_ANN_URL.format(ticker=code) + "?" + urllib.parse.urlencode({"count": str(count)})


def iter_announcements(payload):
    """Accept the ASX company JSON list and the items-wrapper shape."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict):
        rows = data.get("items") or []
    elif isinstance(data, list):
        rows = data
    elif isinstance(payload, list):
        rows = payload
    else:
        rows = []
    for row in rows:
        headline = row.get("headline") or row.get("header") or ""
        url = row.get("url") or ""
        if not url:
            continue
        yield {
            "headline": headline,
            "url": url,
            "date": row.get("date") or row.get("document_release_date") or row.get("document_date"),
        }


def asx_form_from_headline(headline):
    text = (headline or "").casefold()
    if _form_number(text, "605") or "ceas" in text:
        return "605"
    if _form_number(text, "604") or "change in substantial" in text or "change of interests of substantial" in text:
        return "604"
    if _form_number(text, "603") or "becoming a substantial" in text or "initial substantial" in text:
        return "603"
    return None


def choose_primary_name(names):
    if "primary_doc.xml" in names:
        return "primary_doc.xml"
    xmls = [name for name in names if name.lower().endswith(".xml") and "primary" in name.lower()]
    return xmls[0] if xmls else None


def archive_base(cik, accession):
    cik_path = str(int(str(cik).lstrip("0") or "0"))
    acc = accession.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik_path}/{acc}"


def _index_names(index):
    directory = index.get("directory") or {}
    items = directory.get("item") or []
    if isinstance(items, dict):
        items = [items]
    return [item.get("name") or "" for item in items]


def _require_sec(url):
    _require_host(url, ("sec.gov",))


def _require_asx(url):
    _require_host(url, ("asx.com.au",))


def _require_markit(url):
    host = (urllib.parse.urlsplit(url).hostname or "").lower().rstrip(".")
    official = host == "asx.api.markitdigital.com" or host == "asx.com.au" or host.endswith(".asx.com.au")
    if not official:
        raise EndpointError(f"refusing non-official host for {url}")


def _require_host(url, suffixes):
    host = (urllib.parse.urlsplit(url).hostname or "").lower().rstrip(".")
    if not any(host == suffix or host.endswith("." + suffix) for suffix in suffixes):
        raise EndpointError(f"refusing non-official host for {url}")


def _asx_text(body):
    raw = body.encode("utf-8") if isinstance(body, str) else body
    if raw.startswith(b"%PDF"):
        return _pdf_text(raw)
    return _decode(raw)


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


def _decode(body):
    if isinstance(body, str):
        return body
    return body.decode("utf-8", errors="replace")


def _iso_prefix(value):
    if not value:
        return None
    text = str(value).strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    return None


def _form_number(text, number):
    return re.search(rf"\b{number}\b", text) is not None
