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
EXCHANGE_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
ASX_ANN_URL = "https://www.asx.com.au/asx/1/company/{ticker}/announcements"
_WATCHLIST = Path(__file__).resolve().parent / "universe_sec.json"
_OTC_EXCHANGES = {
    "OTC",
    "OTCBB",
    "PINK",
    "GREY",
    "OTC MARKETS",
    "OTCMKTS",
    "OTCQB",
    "OTCQX",
    "OTC PINK",
}
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


def fetch_ticker_map(client=None, watchlist=None):
    """Map a 10-digit issuer CIK to one common-stock ticker.

    Warrant, unit, and OTC-only lines are skipped unless that ticker is on
    the watchlist. When a CIK has both a common line and a warrant, the
    common line wins. The exchange file is optional; the company ticker file
    is enough when it already names the common stock.
    """
    client = client or sec_client()
    _require_sec(TICKERS_URL)
    rows = _ticker_rows(client.json(TICKERS_URL))
    extra = _optional_json(client, EXCHANGE_URL)
    if extra:
        rows.extend(_ticker_rows(extra))
    return choose_by_cik(rows, watchlist if watchlist is not None else load_watchlist())


def load_watchlist(path=None):
    file = Path(path or _WATCHLIST)
    if not file.exists():
        return []
    payload = json.loads(file.read_text(encoding="utf-8"))
    found = []
    for ticker in payload.get("tickers") or []:
        found.append(ticker)
    for company in payload.get("companies") or []:
        if isinstance(company, dict):
            found.append(company.get("ticker") or "")
        else:
            found.append(company)
    return [str(ticker).strip().upper() for ticker in found if str(ticker).strip()]


def tickers_from_payload(payload, watchlist=None):
    return choose_by_cik(_ticker_rows(payload), watchlist or [])


def choose_by_cik(rows, watchlist=None):
    watch = {str(ticker).strip().upper() for ticker in (watchlist or []) if str(ticker).strip()}
    grouped = {}
    for row in rows:
        grouped.setdefault(row["cik"], []).append(row)
    chosen = {}
    for cik, listings in grouped.items():
        ticker = choose_listing(listings, watch)
        if ticker:
            chosen[cik] = ticker
    return chosen


def choose_listing(rows, watchlist=None):
    """Prefer a common-stock ticker. Keep a warrant, unit, or OTC line only when listed."""
    watch = watchlist if isinstance(watchlist, set) else {
        str(ticker).strip().upper() for ticker in (watchlist or []) if str(ticker).strip()
    }
    commons = []
    fallback = []
    seen = set()
    for row in rows:
        ticker = (row.get("ticker") or "").upper()
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        kind = classify_ticker(ticker, row.get("exchange"))
        if kind == "common":
            commons.append(ticker)
        elif ticker in watch:
            fallback.append(ticker)
    if commons:
        commons.sort(key=lambda ticker: (len(ticker), ticker))
        return commons[0]
    return fallback[0] if fallback else None


def classify_ticker(ticker, exchange=None):
    """common, warrant, unit, or otc. NUAIW is a warrant. FMTOF is OTC foreign."""
    symbol = (ticker or "").upper().strip()
    venue = (exchange or "").upper().strip()
    if not symbol:
        return "unknown"
    if _is_warrant(symbol):
        return "warrant"
    if _is_unit(symbol):
        return "unit"
    if venue in _OTC_EXCHANGES or _is_otc_foreign(symbol):
        return "otc"
    return "common"


def _is_warrant(symbol):
    if symbol.endswith(("WS", ".WS", "-WS", "WT", ".WT", "-WT", "WW", ".W", "-W")):
        return True
    # Nasdaq warrant lines are the common symbol plus W (NUAIW). A 1-4 letter
    # name that happens to end in W is left as common stock.
    return len(symbol) >= 5 and symbol.endswith("W") and symbol[:-1].isalnum()


def _is_unit(symbol):
    if symbol.endswith((".U", "-U", ".UN", "-UN")):
        return True
    if symbol.endswith("UN") and len(symbol) >= 4:
        return True
    return len(symbol) >= 5 and symbol.endswith("U") and symbol[:-1].isalnum()


def _is_otc_foreign(symbol):
    return len(symbol) == 5 and symbol.endswith("F") and symbol.isalpha()


def _ticker_rows(payload):
    if not payload:
        return []
    if isinstance(payload, dict) and "fields" in payload and "data" in payload:
        return _rows_from_exchange(payload)
    if isinstance(payload, dict):
        return _rows_from_company_file(payload)
    return []


def _rows_from_company_file(payload):
    rows = []
    for row in payload.values():
        if not isinstance(row, dict):
            continue
        try:
            cik = str(int(row["cik_str"])).zfill(10)
        except (KeyError, TypeError, ValueError):
            continue
        ticker = (row.get("ticker") or "").upper()
        if ticker:
            rows.append({"cik": cik, "ticker": ticker, "exchange": row.get("exchange")})
    return rows


def _rows_from_exchange(payload):
    fields = [str(name).casefold() for name in (payload.get("fields") or [])]
    try:
        cik_at = fields.index("cik")
        ticker_at = fields.index("ticker")
    except ValueError:
        return []
    exchange_at = fields.index("exchange") if "exchange" in fields else None
    rows = []
    for item in payload.get("data") or []:
        if not isinstance(item, (list, tuple)):
            continue
        try:
            cik = str(int(item[cik_at])).zfill(10)
        except (TypeError, ValueError, IndexError):
            continue
        try:
            ticker = str(item[ticker_at] or "").upper()
        except IndexError:
            continue
        exchange = None
        if exchange_at is not None and exchange_at < len(item):
            exchange = item[exchange_at]
        if ticker:
            rows.append({"cik": cik, "ticker": ticker, "exchange": exchange})
    return rows


def _optional_json(client, url):
    try:
        _require_sec(url)
        return client.json(url)
    except Exception:
        return None


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
    if (
        _form_number(text, "604")
        or "change in substantial" in text
        or "change of interests of substantial" in text
        or "change in interests of substantial" in text
    ):
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
