"""Fetch Form 4 filings and ASX Appendix 3Y notices through shared.nethttp.

Pass a client in tests. With no client, SEC calls use sec_client and ASX
calls use asx_client. SEC is refused before any socket when SEC_CONTACT_EMAIL
is missing.
"""

import re
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[1]
for _entry in (str(_REPO), str(_HERE)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from shared.nethttp import EndpointError, asx_client, sec_client

EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
ASX_API = "https://asx.api.markitdigital.com/asx-research/1.0"
FORMS = ("4", "4/A")
_PAGE = 100
_3Y = re.compile(r"appendix\s*3y\b|change of director(?:'s|s)? interest", re.I)


def collect_sec(start, end, client=None, ticker_map=None, limit=None):
    client = client or sec_client()
    ticker_map = ticker_map or {}
    documents = []
    for hit in iter_hits(client, start, end):
        filing = filing_from_hit(hit.get("_source") or {}, ticker_map)
        if filing is None:
            continue
        url, body = fetch_primary(client, filing["cik"], filing["accession"])
        documents.append(
            {
                "source": "sec",
                "form": filing["form"],
                "body": body or "",
                "url": url or filing["document_url"],
                "filing_date": filing["filed"],
                "ticker": filing["ticker"],
                "company": None,
            }
        )
        if limit is not None and len(documents) >= limit:
            break
    return documents


def collect_asx(companies, client=None, count=5):
    """Appendix 3Y notices. The document body is downloaded here.

    The feed returns only the latest five announcements per company.
    """
    client = client or asx_client()
    count = min(int(count), 5)
    documents = []
    for company in companies:
        code = (company.get("ticker") or "").strip().upper()
        if not code:
            continue
        url = f"{ASX_API}/companies/{code.lower()}/announcements?count={int(count)}"
        _require_asx(url)
        payload = client.json(url)
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or "items" not in data:
            raise EndpointError(f"ASX announcements feed for {code} failed the health check")
        name = data.get("displayName") or company.get("name") or code
        for item in (data.get("items") or [])[:count]:
            headline = (item.get("headline") or "").strip()
            key = item.get("documentKey") or ""
            if not headline or not key or not _3Y.search(headline):
                continue
            doc_url = f"{ASX_API}/file/{key}"
            _require_asx(doc_url)
            body = fetch_asx_text(client, doc_url)
            filed = str(item.get("date") or "")[:10] or None
            documents.append(
                {
                    "source": "asx",
                    "form": "3Y",
                    "body": body,
                    "url": doc_url,
                    "filing_date": filed,
                    "ticker": f"{code}.AX",
                    "company": name,
                }
            )
    return documents


def iter_hits(client, start, end):
    offset = 0
    while True:
        url = efts_url(start, end, offset)
        _require_sec(url)
        page = client.json(url)
        hits = ((page.get("hits") or {}).get("hits")) or []
        for hit in hits:
            yield hit
        total = ((page.get("hits") or {}).get("total") or {}).get("value", 0)
        offset += len(hits)
        if not hits or offset >= total:
            return


def efts_url(start, end, offset=0):
    query = urllib.parse.urlencode(
        {
            "q": "*",
            "dateRange": "custom",
            "startdt": start,
            "enddt": end,
            "forms": ",".join(FORMS),
            "from": str(offset),
        }
    )
    return f"{EFTS_URL}?{query}"


def filing_from_hit(source, ticker_map=None):
    form = (source.get("form") or "").upper().replace(" ", "")
    if form not in {"4", "4/A"}:
        return None
    ciks = source.get("ciks") or []
    accession = source.get("adsh") or ""
    if not ciks or not accession:
        return None
    cik = str(int(str(ciks[0]))).zfill(10)
    ticker_map = ticker_map or {}
    ticker = ticker_from_display(source.get("display_names")) or ticker_map.get(cik)
    filed = source.get("file_date") or source.get("display_date_filed")
    return {
        "accession": accession,
        "cik": cik,
        "ticker": ticker,
        "form": form,
        "filed": (str(filed)[:10] if filed else None),
        "document_url": archive_index_url(cik, accession),
    }


def fetch_primary(client, cik, accession):
    base = archive_base(cik, accession)
    index_url = base + "/index.json"
    _require_sec(index_url)
    index = client.json(index_url)
    name = choose_form4_name(_index_names(index))
    if not name:
        return None, None
    url = base + "/" + name
    _require_sec(url)
    body, _content_type = client.get(url, accept="application/xml,text/xml,text/plain")
    return url, _decode(body)


def fetch_asx_text(client, url):
    _require_asx(url)
    body, _content_type = client.get(url, accept="application/pdf,text/html,text/plain")
    if isinstance(body, str):
        body = body.encode("utf-8")
    if body.startswith(b"%PDF"):
        return _pdf_text(body)
    return _decode(body)


def choose_form4_name(names):
    xmls = [name for name in names if name.lower().endswith(".xml")]
    plain = [name for name in xmls if "xsl" not in name.lower()]
    for name in plain + xmls:
        lower = name.lower()
        if any(token in lower for token in ("form4", "primary", "ownership", "doc4")):
            return name
    if plain:
        return plain[0]
    return xmls[0] if xmls else None


def ticker_from_display(display_names):
    if isinstance(display_names, str):
        names = [display_names]
    else:
        names = list(display_names or [])
    for name in names:
        for match in re.finditer(r"\(([^)]+)\)", str(name)):
            token = match.group(1).strip()
            if token.upper().startswith("CIK"):
                continue
            if re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", token):
                return token
    return None


def archive_base(cik, accession):
    cik_path = str(int(str(cik).lstrip("0") or "0"))
    return f"https://www.sec.gov/Archives/edgar/data/{cik_path}/{accession.replace('-', '')}"


def archive_index_url(cik, accession):
    acc = (accession or "").strip()
    return f"{archive_base(cik, acc)}/{acc}-index.html"


def _index_names(index):
    directory = (index or {}).get("directory") or {}
    items = directory.get("item") or []
    if isinstance(items, dict):
        items = [items]
    return [item.get("name") or "" for item in items]


def _require_sec(url):
    host = _hostname(url)
    if host != "sec.gov" and not host.endswith(".sec.gov"):
        raise EndpointError(f"refusing non-official host for {url}")


def _require_asx(url):
    host = _hostname(url)
    official = host == "asx.com.au" or host.endswith(".asx.com.au") or host == "asx.api.markitdigital.com"
    if not official:
        raise EndpointError(f"refusing non-official host for {url}")


def _hostname(url):
    return (urllib.parse.urlsplit(url).hostname or "").lower().rstrip(".")


def _pdf_text(body):
    with tempfile.TemporaryDirectory() as tmp:
        pdf_path = Path(tmp) / "notice.pdf"
        txt_path = Path(tmp) / "notice.txt"
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
            raise EndpointError("pdftotext failed on Appendix 3Y")
        return txt_path.read_text(encoding="utf-8", errors="replace")


def _decode(body):
    if isinstance(body, str):
        return body
    return body.decode("utf-8", errors="replace")
