"""List 8-K and 424B filings from the EDGAR index, and ASX debt announcements.

The cheap filter scores the index description or the ASX headline. The pipeline
fetches a primary document only when that score is above the threshold or the
filter cannot decide. Pass a client in tests. With no client, SEC calls use
sec_client and ASX calls use asx_client.
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
_DEBT_HEADLINE = re.compile(
    r"\b(debt|loan|facility|facilities|bond|notes|refinanc\w*|credit|borrow\w*)\b",
    re.I,
)
FORMS = (
    "8-K",
    "424B1",
    "424B2",
    "424B3",
    "424B4",
    "424B5",
    "424B7",
    "424B8",
)
EIGHT_K_ITEMS = frozenset({"1.01", "2.03"})
_PAGE = 100


def collect(start, end, client=None, ticker_map=None, limit=None, tickers=None):
    """In-scope filings for the window. Summary text is the index description."""
    client = client or sec_client()
    ticker_map = ticker_map or {}
    wanted = _ticker_set(tickers)
    filings = []
    for hit in iter_hits(client, start, end):
        filing = filing_from_hit(hit.get("_source") or {}, ticker_map)
        if filing is None:
            continue
        if wanted and (filing.get("ticker") or "").upper() not in wanted:
            continue
        filings.append(filing)
        if limit is not None and len(filings) >= limit:
            break
    return filings


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
    """EDGAR full-text search. A `q=*` term and an `8-K/A` form both return no hits."""
    query = urllib.parse.urlencode(
        {
            "dateRange": "custom",
            "startdt": start,
            "enddt": end,
            "forms": ",".join(FORMS),
            "from": str(offset),
        }
    )
    return f"{EFTS_URL}?{query}"


def filing_from_hit(source, ticker_map=None):
    form = normalize_form(source.get("form"))
    items = split_items(source.get("items"))
    if not in_scope(form, items):
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
        "items": items,
        "filed": (str(filed)[:10] if filed else None),
        "summary": (source.get("file_description") or "").strip(),
        "document_url": archive_index_url(cik, accession),
    }


def fetch_primary_text(client, cik, accession):
    """Download the primary filing text. Called only from the full-document pass."""
    name, index = _primary_from_index(client, cik, accession)
    if not name:
        return None
    url = archive_base(cik, accession) + "/" + name
    _require_sec(url)
    body, _content_type = client.get(url, accept="text/html,text/plain")
    return _decode(body)


def fetch_phrase_text(client, cik, accession):
    """Index descriptions, or the first page of the primary document.

    EFTS `file_description` is often only the form name, which the phrase
    stage cannot score. Returns `(phrase, full_body_or_none)`.
    """
    name, index = _primary_from_index(client, cik, accession)
    descriptions = []
    for item in _index_items(index):
        desc = (item.get("description") or "").strip()
        if desc and not summary_is_form_label(desc, ""):
            descriptions.append(desc)
    if descriptions:
        return " ".join(descriptions), None
    if not name:
        return "", None
    url = archive_base(cik, accession) + "/" + name
    _require_sec(url)
    body, _content_type = client.get(url, accept="text/html,text/plain")
    text = _decode(body)
    return first_page(text), text


def _primary_from_index(client, cik, accession):
    base = archive_base(cik, accession)
    index_url = base + "/index.json"
    _require_sec(index_url)
    index = client.json(index_url)
    return choose_primary_name(_index_names(index)), index


def collect_asx(companies, client=None, count=5):
    """Recent ASX debt headlines. The PDF is not downloaded here."""
    client = client or asx_client()
    filings = []
    for company in companies:
        code = (company.get("ticker") or "").strip().upper()
        if not code:
            continue
        url = f"{ASX_API}/companies/{code.lower()}/announcements?count={int(count)}"
        _require_asx(url)
        payload = client.json(url)
        data = payload.get("data") or {}
        name = data.get("displayName") or company.get("name") or code
        for item in (data.get("items") or [])[:count]:
            headline = (item.get("headline") or "").strip()
            key = item.get("documentKey") or ""
            if not headline or not key or not _DEBT_HEADLINE.search(headline):
                continue
            filed = item.get("date") or ""
            doc_url = f"{ASX_API}/file/{key}"
            filings.append(
                {
                    "accession": key,
                    "cik": "",
                    "ticker": f"{code}.AX",
                    "company": name,
                    "form": "ASX",
                    "items": [],
                    "filed": str(filed)[:10] or None,
                    "summary": headline,
                    "document_url": doc_url,
                    "source_type": "asx_announcement",
                    "market": "ASX",
                }
            )
    return filings


def fetch_asx_text(client, url):
    """Download an ASX announcement and return its text. PDFs go through pdftotext."""
    _require_asx(url)
    body, _content_type = client.get(url, accept="application/pdf,text/html,text/plain")
    if isinstance(body, str):
        body = body.encode("utf-8")
    if body.startswith(b"%PDF"):
        return _pdf_text(body)
    return _decode(body)


def in_scope(form, items):
    form = normalize_form(form)
    if form == "ASX":
        return True
    if form.startswith("424B"):
        return True
    if form == "8-K":
        return bool(set(split_items(items)) & EIGHT_K_ITEMS)
    return False


def normalize_form(form):
    """8-K and 8-K/A stay 8-K. 424B-5 and 424B5/A stay 424B5."""
    text = (form or "").upper().replace(" ", "")
    if text.endswith("/A"):
        text = text[:-2]
    compact = text.replace("-", "")
    if compact == "8K":
        return "8-K"
    return compact


def split_items(value):
    if isinstance(value, (list, tuple)):
        raw = ",".join(str(part) for part in value)
    else:
        raw = str(value or "")
    return re.findall(r"\d+\.\d+", raw)


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


_REPORT_PAGE = re.compile(r"(?:^|/)r\d+\.html?$", re.I)
_SUBMISSION_TXT = re.compile(r"(?:^|/)(?:\d{10}-\d{2}-\d{6}|\d{18})\.txt$", re.I)
_FORM_LABEL = re.compile(r"^(?:FORM)?(?:8K|424B\d+)$")


def summary_is_form_label(summary, form):
    """True when the text is empty or only the form name EFTS returns."""
    compact = re.sub(r"[^A-Za-z0-9]", "", summary or "").upper()
    if not compact:
        return True
    form_compact = re.sub(r"[^A-Za-z0-9]", "", form or "").upper()
    if form_compact and compact in {form_compact, "FORM" + form_compact}:
        return True
    return _FORM_LABEL.fullmatch(compact) is not None


def first_page(text, limit=4000):
    plain = re.sub(r"<[^>]+>", " ", text or "")
    plain = re.sub(r"\s+", " ", plain).strip()
    return plain[:limit]


def choose_primary_name(names):
    """Primary HTML/text, skipping index pages, R<n>.htm renders, and the submission txt."""
    files = [name for name in names if _keep_primary_name(name)]
    for name in files:
        if "primary" in name.lower():
            return name
    body = []
    for name in files:
        lower = name.lower()
        if "exhibit" in lower or "ex99" in lower or re.search(r"ex-?\d", lower):
            continue
        body.append(name)
    if body:
        return body[0]
    return files[0] if files else None


def _keep_primary_name(name):
    lower = name.lower()
    if "-index" in lower:
        return False
    if _REPORT_PAGE.search(lower) or _SUBMISSION_TXT.search(lower):
        return False
    return lower.endswith((".htm", ".html", ".txt"))


def _ticker_set(tickers):
    if not tickers:
        return None
    wanted = set()
    for ticker in tickers:
        token = str(ticker).strip().upper()
        if token.endswith(".AX"):
            token = token[:-3]
        if token:
            wanted.add(token)
    return wanted or None


def archive_base(cik, accession):
    cik_path = str(int(str(cik).lstrip("0") or "0"))
    return f"https://www.sec.gov/Archives/edgar/data/{cik_path}/{accession.replace('-', '')}"


def archive_index_url(cik, accession):
    acc = (accession or "").strip()
    return f"{archive_base(cik, acc)}/{acc}-index.html"


def _index_items(index):
    directory = (index or {}).get("directory") or {}
    items = directory.get("item") or []
    if isinstance(items, dict):
        items = [items]
    return items


def _index_names(index):
    return [item.get("name") or "" for item in _index_items(index)]


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
        pdf_path = str(Path(tmp) / "announcement.pdf")
        txt_path = str(Path(tmp) / "announcement.txt")
        Path(pdf_path).write_bytes(body)
        try:
            completed = subprocess.run(
                ["pdftotext", "-layout", pdf_path, txt_path],
                check=False,
                capture_output=True,
            )
        except FileNotFoundError as exc:
            raise EndpointError("pdftotext is not installed") from exc
        if completed.returncode != 0 or not Path(txt_path).exists():
            raise EndpointError("pdftotext failed on ASX announcement")
        return Path(txt_path).read_text(encoding="utf-8", errors="replace")


def _decode(body):
    if isinstance(body, str):
        return body
    return body.decode("utf-8", errors="replace")
