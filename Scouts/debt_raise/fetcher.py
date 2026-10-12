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
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
ASX_API = "https://asx.api.markitdigital.com/asx-research/1.0"
_DEBT_HEADLINE = re.compile(
    r"\b(debt|loan|facility|facilities|bond|notes|refinanc\w*|credit|borrow\w*)\b",
    re.I,
)
# 8-K/A is left out of the search. Amendments are not part of this pull.
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


def collect_window(start, end, tickers=None, client=None, limit=None):
    """In-scope filings for the window, scoped by issuer CIK when tickers are set.

    Tickers are resolved through the SEC company ticker file. The search uses
    the efts ``ciks`` parameter and does not drop a hit because its display
    name has no ticker. A 424B filing often has none; the ticker is filled
    from the CIK. Returns ``(filings, unmapped_tickers)``.

    Pass a client in tests. With no client, SEC calls use sec_client.
    """
    client = client or sec_client()
    cik_to_ticker, ticker_to_cik = fetch_ticker_tables(client)
    ciks = None
    missing = []
    if tickers is not None:
        ciks, missing, preferred = resolve_tickers(tickers, ticker_to_cik)
        if not ciks:
            return [], missing
        cik_to_ticker.update(preferred)
    filings = collect(
        start,
        end,
        client=client,
        ticker_map=cik_to_ticker,
        limit=limit,
        ciks=ciks,
    )
    return filings, missing


def collect(start, end, client=None, ticker_map=None, limit=None, ciks=None):
    """In-scope filings for the window. Summary text is the index description.

    ``ciks`` limits the search to those issuers. Hits are kept by CIK, not by
    the ticker text on the hit.
    """
    client = client or sec_client()
    ticker_map = ticker_map or {}
    filings = []
    for hit in iter_hits(client, start, end, ciks=ciks):
        filing = filing_from_hit(hit.get("_source") or {}, ticker_map, ciks=ciks)
        if filing is None:
            continue
        filings.append(filing)
        if limit is not None and len(filings) >= limit:
            break
    return filings


def iter_hits(client, start, end, ciks=None):
    offset = 0
    while True:
        url = efts_url(start, end, offset, ciks=ciks)
        _require_sec(url)
        page = client.json(url)
        hits = ((page.get("hits") or {}).get("hits")) or []
        for hit in hits:
            yield hit
        total = ((page.get("hits") or {}).get("total") or {}).get("value", 0)
        offset += len(hits)
        if not hits or offset >= total:
            return


def efts_url(start, end, offset=0, ciks=None):
    """EDGAR search for the window.

    ``q`` is omitted. A ``q`` of ``*`` is a literal asterisk, not a match-all,
    and the search comes back empty. Company scope is the ``ciks`` parameter.
    """
    params = {
        "dateRange": "custom",
        "startdt": start,
        "enddt": end,
        "forms": ",".join(FORMS),
        "from": str(offset),
    }
    if ciks:
        params["ciks"] = ",".join(_cik10(cik) for cik in ciks)
    query = urllib.parse.urlencode(params, safe=",")
    return f"{EFTS_URL}?{query}"


def fetch_ticker_tables(client):
    """Map 10-digit CIKs to tickers, and tickers to CIKs, from the SEC ticker file."""
    _require_sec(TICKERS_URL)
    payload = client.json(TICKERS_URL)
    rows = payload.values() if isinstance(payload, dict) else payload
    cik_to_ticker = {}
    ticker_to_cik = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        try:
            cik = _cik10(row.get("cik_str"))
        except (TypeError, ValueError):
            continue
        ticker = (row.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        cik_to_ticker.setdefault(cik, ticker)
        ticker_to_cik.setdefault(ticker, cik)
    return cik_to_ticker, ticker_to_cik


def resolve_tickers(tickers, ticker_to_cik):
    """Split requested symbols into known CIKs and symbols the file does not list.

    The preferred ticker for a CIK is the one that was requested, so a 424B
    hit is labeled with that symbol even when the index omits it.
    """
    ciks = []
    missing = []
    preferred = {}
    seen = set()
    for ticker in tickers or []:
        key = (ticker or "").strip().upper()
        if not key:
            continue
        cik = ticker_to_cik.get(key)
        if not cik:
            missing.append(key)
            continue
        preferred.setdefault(cik, key)
        if cik not in seen:
            seen.add(cik)
            ciks.append(cik)
    return ciks, missing, preferred


def filing_from_hit(source, ticker_map=None, ciks=None):
    form = normalize_form(source.get("form"))
    items = split_items(source.get("items"))
    if not in_scope(form, items):
        return None
    present = _cik_list(source.get("ciks"))
    accession = source.get("adsh") or ""
    if not present or not accession:
        return None
    allowed = {_cik10(cik) for cik in ciks} if ciks else None
    if allowed is not None:
        matched = [cik for cik in present if cik in allowed]
        if not matched:
            return None
        cik = matched[0]
    else:
        cik = present[0]
    ticker_map = ticker_map or {}
    # The company ticker file wins. 424B display names often have no ticker,
    # and a paren in the name is not a reliable symbol.
    ticker = ticker_map.get(cik) or ticker_from_display(source.get("display_names"))
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
    base = archive_base(cik, accession)
    index_url = base + "/index.json"
    _require_sec(index_url)
    index = client.json(index_url)
    name = choose_primary_name(_index_names(index))
    if not name:
        return None
    url = base + "/" + name
    _require_sec(url)
    body, _content_type = client.get(url, accept="text/html,text/plain")
    return _decode(body)


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


def choose_primary_name(names):
    files = [name for name in names if name.lower().endswith((".htm", ".html", ".txt"))]
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


def _cik10(value):
    return str(int(str(value).strip())).zfill(10)


def _cik_list(values):
    found = []
    for value in values or []:
        try:
            found.append(_cik10(value))
        except (TypeError, ValueError):
            continue
    return found


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
