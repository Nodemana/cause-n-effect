"""Named counterparties from official SEC filings.

Capex leads take suppliers and partners from recent 8-K Item 1.01 and 8.01
filings and the latest 10-K (purchase obligations, named vendors). Backlog
leads take customers from 8-K contract announcements and 10-K customer
concentration notes. The caller passes a sec_client(); the User-Agent is
that client's, built from SEC_CONTACT_EMAIL. This module never logs the
address. A submissions, full-text, or document error drops every party for
that company.
"""

import html
import logging
import re
import urllib.parse
from datetime import date, timedelta

from shared.nethttp import EndpointError, sec_contact_email

from pipeline.load import extract_cik
from pipeline.names import normalise_name

log = logging.getLogger(__name__)

EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
EFTS_FORMS = "8-K,10-K"
LOOKBACK_DAYS = 540
_PARTY_CAP = 8
_EIGHT_K_LIMIT = 3

CAPEX_8K_ITEMS = frozenset({"1.01", "8.01"})
BACKLOG_8K_ITEMS = frozenset({"1.01", "8.01"})
CAPEX_QUERY = '"purchase obligation" OR "supply agreement" OR vendor OR supplier'
BACKLOG_QUERY = '"accounted for" OR "customer concentration" OR "contract with"'

_EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_COMMENT = re.compile(r"(?is)<!--.*?-->")
_SCRIPT = re.compile(r"(?is)<script\b.*?>.*?</script>")
_STYLE = re.compile(r"(?is)<style\b.*?>.*?</style>")
_TAG = re.compile(r"(?is)<[^>]+>")
_WORD = r"[A-Z][A-Za-z0-9&'’-]*"
_NAME = rf"({_WORD}(?:[ \t]+{_WORD}){{0,3}})"
_SUPPLIER_PATTERNS = (
    re.compile(r"(?i:\bpurchase obligations?\s+(?:with|to|from)\s+)" + _NAME),
    re.compile(r"(?i:\b(?:supply|purchase|procurement)\s+agreement\s+with\s+)" + _NAME),
    re.compile(r"(?i:\b(?:suppliers?|vendors?|partners?)\s+)" + _NAME),
    re.compile(r"(?i:\bagreement\s+with\s+)" + _NAME + r"(?i:\s+to\s+(?:purchase|supply|provide)\b)"),
)
_CUSTOMER_PATTERNS = (
    re.compile(r"(?i:\bcustomer\s+)" + _NAME + r"(?i:\s+accounted\s+for\s+\d)"),
    re.compile(
        _NAME
        + r"(?i:\s+accounted\s+for\s+\d+(?:\.\d+)?\s*(?:%|percent)\s+of\s+"
        r"(?:(?:our|the|its)\s+)?(?:(?:total|consolidated|net)\s+)?revenues?\b)"
    ),
    re.compile(r"(?i:\bcontract\s+with\s+)" + _NAME),
)
_LEADING = {"the", "our", "its", "a", "an", "and", "or", "of", "for", "to", "with"}
_SUFFIXES = {
    "inc",
    "incorporated",
    "corp",
    "corporation",
    "llc",
    "ltd",
    "limited",
    "plc",
    "co",
    "company",
    "lp",
    "sa",
    "nv",
    "ag",
}
_STOP = {
    "company",
    "item",
    "revenue",
    "revenues",
    "customers",
    "customer",
    "suppliers",
    "supplier",
    "vendors",
    "vendor",
    "partners",
    "partner",
    "total",
    "consolidated",
    "net",
    "purchase",
    "purchases",
    "obligations",
    "obligation",
    "agreement",
    "contract",
    "percent",
    "approximately",
    "various",
    "named",
    "following",
    "below",
    "above",
    "note",
    "notes",
    "exhibit",
    "form",
    "sec",
    "other",
    "certain",
}


def submissions_url(cik):
    return f"https://data.sec.gov/submissions/CIK{_cik10(cik)}.json"


def efts_url(cik, query, start, end):
    """EDGAR full-text search for one company and one query."""
    params = urllib.parse.urlencode(
        {
            "q": query,
            "dateRange": "custom",
            "startdt": start,
            "enddt": end,
            "forms": EFTS_FORMS,
            "ciks": _cik10(cik),
        },
        safe=",",
    )
    return f"{EFTS_URL}?{params}"


def archive_document_url(cik, accession, document):
    cik_path = str(int(str(cik)))
    acc = str(accession).replace("-", "")
    name = str(document).lstrip("/")
    return f"https://www.sec.gov/Archives/edgar/data/{cik_path}/{acc}/{name}"


def search_window(as_of):
    """Inclusive filing dates from as_of back LOOKBACK_DAYS."""
    if as_of is None:
        from shared.store import utc_now

        as_of = utc_now()
    if isinstance(as_of, date):
        end = as_of
    else:
        end = date.fromisoformat(str(as_of)[:10])
    start = end - timedelta(days=LOOKBACK_DAYS)
    return start.isoformat(), end.isoformat()


def enrich_leads(leads, *, client=None, names=None, as_of=None):
    """Attach SEC counterparties to capex and backlog leads.

    `client` is a sec_client(). With no client the leads are left without
    SEC parties. One failed source drops every party for that company.
    """
    groups = {}
    for lead in leads:
        lead.counterparties = []
        if client is None or lead.lead_type not in {"capex", "orders_backlog"}:
            continue
        cik = extract_cik(getattr(lead, "source_url", None) or "")
        if not cik:
            continue
        groups.setdefault(cik, []).append(lead)
    if client is None:
        return
    for cik, group in groups.items():
        suppliers = any(lead.lead_type == "capex" for lead in group)
        customers = any(lead.lead_type == "orders_backlog" for lead in group)
        try:
            found = _company_parties(client, cik, suppliers, customers, as_of, names or {})
        except EndpointError:
            _log_closed(group)
            continue
        for lead in group:
            want = "supplier" if lead.lead_type == "capex" else "customer"
            lead.counterparties = [dict(party) for party in found if party["role"] == want]


def parties_from_text(text, role):
    """Clean party names of one role in already-plain filing text."""
    patterns = _SUPPLIER_PATTERNS if role == "supplier" else _CUSTOMER_PATTERNS
    if role not in {"supplier", "customer"}:
        return []
    found = []
    seen = set()
    plain = _plain(text)
    for pattern in patterns:
        for match in pattern.finditer(plain):
            name = _clean_name(match.group(1))
            key = normalise_name(name)
            if not key or key in seen:
                continue
            seen.add(key)
            found.append(name)
    return found


def select_filings(payload, *, suppliers, customers, start, end):
    """Newest matching 8-Ks, up to three, plus the latest 10-K in the window."""
    recent = ((payload or {}).get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    wanted = set()
    if suppliers:
        wanted |= CAPEX_8K_ITEMS
    if customers:
        wanted |= BACKLOG_8K_ITEMS
    eight = []
    tenk = []
    for index, form in enumerate(forms):
        filed = _at(recent, "filingDate", index)
        accession = _at(recent, "accessionNumber", index)
        document = _at(recent, "primaryDocument", index)
        if not filed or not accession or not document:
            continue
        if filed < start or filed > end:
            continue
        row = {
            "form": form,
            "accession": accession,
            "filing_date": filed,
            "primary_document": document,
            "items": _split_items(_at(recent, "items", index)),
            "index": index,
        }
        if form == "8-K" and wanted and (set(row["items"]) & wanted):
            eight.append(row)
        elif form == "10-K":
            tenk.append(row)
    eight.sort(key=lambda row: (row["filing_date"], -row["index"]), reverse=True)
    tenk.sort(key=lambda row: (row["filing_date"], -row["index"]), reverse=True)
    chosen = []
    seen = set()
    for row in eight[:_EIGHT_K_LIMIT] + tenk[:1]:
        if row["accession"] in seen:
            continue
        seen.add(row["accession"])
        chosen.append(row)
    return chosen


def _company_parties(client, cik, suppliers, customers, as_of, names):
    start, end = search_window(as_of)
    sub_url = submissions_url(cik)
    _require_sec(sub_url)
    payload = client.json(sub_url)
    if not isinstance(payload, dict):
        raise EndpointError("submissions payload was not an object")
    texts = []
    for filing in select_filings(payload, suppliers=suppliers, customers=customers, start=start, end=end):
        url = archive_document_url(cik, filing["accession"], filing["primary_document"])
        _require_sec(url)
        body, _content_type = client.get(url, accept="text/html,text/plain")
        texts.append((url, _plain(body)))
    queries = []
    if suppliers:
        queries.append(CAPEX_QUERY)
    if customers:
        queries.append(BACKLOG_QUERY)
    for query in queries:
        url = efts_url(cik, query, start, end)
        _require_sec(url)
        page = client.json(url)
        if not isinstance(page, dict):
            raise EndpointError("full-text payload was not an object")
        texts.extend(_snippets(cik, page))
    return _collect(texts, suppliers, customers, names)


def _collect(texts, suppliers, customers, names):
    found = []
    seen = set()
    counts = {"supplier": 0, "customer": 0}
    for url, text in texts:
        pairs = []
        if suppliers:
            pairs.extend(("supplier", name) for name in parties_from_text(text, "supplier"))
        if customers:
            pairs.extend(("customer", name) for name in parties_from_text(text, "customer"))
        for role, name in pairs:
            if counts[role] >= _PARTY_CAP:
                continue
            key = normalise_name(name)
            identity = (role, key)
            if not key or identity in seen:
                continue
            seen.add(identity)
            counts[role] += 1
            ticker = (names or {}).get(key) or None
            found.append(
                {
                    "role": role,
                    "name": name,
                    "name_normalised": key,
                    "ticker": ticker,
                    "source_url": url,
                }
            )
    return found


def _snippets(cik, page):
    hits = ((page.get("hits") or {}).get("hits")) or []
    found = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        url = _hit_url(cik, hit)
        if not url:
            continue
        _require_sec(url)
        chunks = []
        highlight = hit.get("highlight") or {}
        if isinstance(highlight, dict):
            for value in highlight.values():
                if isinstance(value, list):
                    chunks.extend(str(item) for item in value)
                elif isinstance(value, str):
                    chunks.append(value)
        text = _plain(" ".join(chunks))
        if text:
            found.append((url, text))
    return found


def _hit_url(cik, hit):
    source = hit.get("_source") or {}
    adsh = str(source.get("adsh") or "")
    document = ""
    raw_id = str(hit.get("_id") or "")
    if ":" in raw_id:
        left, right = raw_id.split(":", 1)
        document = right
        if not adsh:
            adsh = left
    if not adsh or not document:
        return None
    return archive_document_url(cik, adsh, document)


def _plain(body):
    if isinstance(body, bytes):
        text = body.decode("utf-8", "replace")
    else:
        text = str(body or "")
    text = html.unescape(text).replace("\xa0", " ")
    text = _COMMENT.sub(" ", text)
    text = _SCRIPT.sub(" ", text)
    text = _STYLE.sub(" ", text)
    text = _TAG.sub(" ", text)
    text = html.unescape(text)
    return " ".join(text.split())


def _clean_name(raw):
    tokens = []
    for token in (raw or "").split():
        token = token.strip(".,;:\"'()[]")
        if token:
            tokens.append(token)
    while tokens and tokens[-1].casefold().rstrip(".") in _SUFFIXES:
        tokens.pop()
    while tokens and tokens[0].casefold() in _LEADING:
        tokens.pop(0)
    while len(tokens) > 1 and tokens[0].casefold() in _STOP:
        tokens.pop(0)
    if not tokens:
        return ""
    if any(token.casefold() in _STOP for token in tokens):
        return ""
    if len(tokens) == 1 and len(tokens[0]) < 3:
        return ""
    return " ".join(tokens)


def _split_items(value):
    return [part.strip() for part in str(value or "").replace(";", ",").split(",") if part.strip()]


def _at(recent, key, index):
    values = recent.get(key) or []
    if index >= len(values) or values[index] is None:
        return ""
    return str(values[index])


def _cik10(cik):
    return str(int(str(cik))).zfill(10)


def _require_sec(url):
    host = (urllib.parse.urlsplit(url).hostname or "").lower().rstrip(".")
    if host != "sec.gov" and not host.endswith(".sec.gov"):
        raise EndpointError("refusing non-SEC host")


def _log_closed(group):
    labels = []
    for lead in group:
        ticker = getattr(lead, "ticker", None) or ""
        if ticker and ticker not in labels:
            labels.append(ticker)
    label = ", ".join(labels) if labels else "unknown"
    log.warning("%s", _redact(f"counterparty enrichment for {label} failed closed"))


def _redact(message):
    text = str(message or "")
    contact = sec_contact_email()
    if contact:
        text = text.replace(contact, "[redacted]")
    return _EMAIL.sub("[redacted]", text)
