"""Named counterparties from official SEC filings.

Capex leads take suppliers from 8-K Item 1.01 agreements and their EX-10
exhibits. Backlog leads take customers from 8-K contract announcements and
10-K customer-concentration notes. A direct EDGAR full-text search also
looks for each backlog company's name inside the capex company's filings
and the other way around.

A name is kept only when it matches a known SEC registrant in
company_tickers.json or it ends in a company suffix, and it is not a
heading or a common word. A match is stored under the registrant's title
and ticker. The caller passes a sec_client(); the User-Agent is that
client's, built from SEC_CONTACT_EMAIL. This module never logs the
address. company_tickers.json is fetched once per enrich_leads call. A
submissions, full-text, index, or document error drops every party for
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
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
EFTS_FORMS = "8-K,10-K"
LOOKBACK_DAYS = 540
_PARTY_CAP = 8
_EIGHT_K_LIMIT = 3
_EX10_LIMIT = 4

CAPEX_8K_ITEMS = frozenset({"1.01", "8.01"})
BACKLOG_8K_ITEMS = frozenset({"1.01", "8.01"})
CAPEX_QUERY = '"agreement with" OR "by and between"'
BACKLOG_QUERY = '"accounted for" OR "largest customer" OR "contract with"'

_EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_COMMENT = re.compile(r"(?is)<!--.*?-->")
_SCRIPT = re.compile(r"(?is)<script\b.*?>.*?</script>")
_STYLE = re.compile(r"(?is)<style\b.*?>.*?</style>")
_TAG = re.compile(r"(?is)<[^>]+>")
_EX10 = re.compile(r"(?i)(?:ex[-_]?10|exhibit[-_]?10)")
_WORD = r"(?:N\.V\.|S\.A\.|[A-Z][A-Za-z0-9&'’–-]*)"
_NAME_CORE = rf"{_WORD}(?:[ \t]+{_WORD}){{0,5}}"
_NAME = rf"({_NAME_CORE})"
_AGREEMENT = re.compile(r"(?i:\bagreement\s+with\s+)" + _NAME)
_CONTRACT = re.compile(r"(?i:\bcontract\s+with\s+)" + _NAME)
_BETWEEN = re.compile(
    r"(?i:\bby\s+and\s+between\s+)" + _NAME + r"(?i:\s+and\s+)" + _NAME
)
_ACCOUNTED = re.compile(
    _NAME + r"(?i:\s+accounted\s+for\s+\d+(?:\.\d+)?\s*(?:%|percent)\s+of\b)"
)
_LARGEST_AFTER = re.compile(
    r"(?i:\blargest\s+customer\b(?:\s*,)?(?:\s+(?:was|is|were))?\s+)" + _NAME
)
_LARGEST_BEFORE = re.compile(_NAME + r"(?i:,\s+(?:our|its|the)\s+largest\s+customer\b)")

_SUFFIXES = {
    "inc",
    "corp",
    "corporation",
    "ltd",
    "limited",
    "llc",
    "plc",
    "ag",
    "co",
}
_DOTTED_SUFFIXES = {"nv", "sa"}
_LEADING = {
    "the",
    "our",
    "its",
    "their",
    "a",
    "an",
    "customer",
    "customers",
    "supplier",
    "suppliers",
}
_EXACT_HEADINGS = {
    "network",
    "during",
    "relationships subject us",
    "agreements we",
    "services enterprise",
    "summary results of operations",
}
_COMMON = {
    "overview",
    "discussion",
    "analysis",
    "management",
    "financial",
    "statements",
    "consolidated",
    "unaudited",
    "item",
    "part",
    "note",
    "notes",
    "table",
    "contents",
    "exhibit",
    "index",
    "signature",
    "page",
    "form",
    "annual",
    "report",
    "business",
    "risk",
    "factors",
    "properties",
    "legal",
    "proceedings",
    "market",
    "controls",
    "quantitative",
    "qualitative",
    "disclosures",
    "following",
    "see",
    "below",
    "above",
    "total",
    "net",
    "revenue",
    "revenues",
    "sales",
    "income",
    "other",
    "certain",
    "various",
    "named",
    "customer",
    "customers",
    "supplier",
    "suppliers",
    "company",
    "companies",
    "the",
    "our",
    "its",
    "their",
    "we",
    "us",
    "and",
    "or",
    "of",
    "for",
    "to",
    "with",
    "by",
    "between",
    "this",
    "that",
    "these",
    "those",
    "a",
    "an",
    "in",
    "on",
    "from",
    "at",
    "as",
    "into",
    "under",
    "over",
    "not",
    "no",
    "any",
    "all",
    "such",
    "may",
    "subject",
    "results",
    "operations",
    "summary",
    "network",
    "during",
    "agreements",
    "agreement",
    "services",
    "service",
    "enterprise",
    "relationships",
    "relationship",
    "vendor",
    "vendors",
    "partner",
    "partners",
    "purchase",
    "purchases",
    "obligation",
    "obligations",
    "contract",
    "percent",
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


def registrant_index(payload):
    """Map normalised titles, suffix-stripped titles, and unique tickers to (title, ticker)."""
    by_name = {}
    by_ticker_rows = {}
    if not isinstance(payload, dict):
        return {"by_name": by_name, "by_ticker": {}}
    for row in payload.values():
        if not isinstance(row, dict):
            continue
        title = " ".join(str(row.get("title") or "").split())
        ticker = str(row.get("ticker") or "").strip().upper()
        if not title or not ticker:
            continue
        record = (title, ticker)
        full = normalise_name(title)
        if full and full not in by_name:
            by_name[full] = record
        stripped = normalise_name(" ".join(_stem_tokens(title)))
        if stripped and stripped not in by_name:
            by_name[stripped] = record
        by_ticker_rows.setdefault(ticker, []).append(record)
    by_ticker = {}
    for ticker, rows in by_ticker_rows.items():
        titles = {row[0] for row in rows}
        if len(titles) != 1:
            continue
        by_ticker[ticker] = rows[0]
        key = normalise_name(ticker)
        if key and key not in by_name:
            by_name[key] = rows[0]
    return {"by_name": by_name, "by_ticker": by_ticker}


def load_registrants(client):
    """Fetch company_tickers.json once. The caller reuses the result for the sweep."""
    _require_sec(TICKERS_URL)
    payload = client.json(TICKERS_URL)
    if not isinstance(payload, dict):
        raise EndpointError("company tickers payload was not an object")
    return registrant_index(payload)


def is_common_heading(name):
    """True when every remaining word is a heading or a common word."""
    text = _core_text(name)
    key = normalise_name(text)
    if not key or key in _EXACT_HEADINGS:
        return True
    tokens = [_fold_token(token) for token in text.split()]
    tokens = [token for token in tokens if token]
    if not tokens:
        return True
    return all(token in _COMMON for token in tokens)


def accept_party(name, registrants=None, names=None):
    """Return a canonical party, or None when the name is not a company.

    A SEC registrant match wins and supplies that registrant's title and
    ticker. Otherwise the name must end in a company suffix and keep at
    least one word that is not a heading or a common word.
    """
    core = _core_text(name)
    if not core:
        return None
    hit = _lookup_registrant(core, registrants)
    if hit is None and core != (name or ""):
        hit = _lookup_registrant(name, registrants)
    if hit:
        title, ticker = hit
        if is_common_heading(title):
            return None
        return _party(title, ticker)
    if is_common_heading(core) or not _has_suffix(core) or _stem_is_common(core):
        return None
    return _party(core, _ticker_from_names(core, names))


def ex10_names(payload):
    """EX-10 filenames from an EDGAR index.json directory listing."""
    items = ((payload or {}).get("directory") or {}).get("item") or []
    if isinstance(items, dict):
        items = [items]
    found = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        base = name.rsplit("/", 1)[-1]
        if base and _EX10.search(base):
            found.append(name)
    return found


def enrich_leads(leads, *, client=None, names=None, as_of=None):
    """Attach SEC counterparties to capex and backlog leads.

    `client` is a sec_client(). With no client the leads are left without
    SEC parties. company_tickers.json is read once for the whole call.
    One failed source drops every party for that company.
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
    if client is None or not groups:
        return
    try:
        registrants = load_registrants(client)
    except EndpointError:
        for group in groups.values():
            _log_closed(group)
        return
    for cik, group in groups.items():
        suppliers = any(lead.lead_type == "capex" for lead in group)
        customers = any(lead.lead_type == "orders_backlog" for lead in group)
        peers = _peers_for(cik, group, groups, registrants)
        try:
            found = _company_parties(
                client,
                cik,
                suppliers,
                customers,
                as_of,
                names or {},
                registrants,
                peers,
                group,
            )
        except EndpointError:
            _log_closed(group)
            continue
        for lead in group:
            want = "supplier" if lead.lead_type == "capex" else "customer"
            lead.counterparties = [
                dict(party)
                for party in found
                if party["role"] == want and not _is_self(party, lead)
            ]


def parties_from_text(text, role, registrants=None, names=None):
    """Accepted party names of one role in filing text.

    Suppliers come from contract language. Customers come from contract
    language and from customer-concentration language. Headings are dropped.
    """
    if role not in {"supplier", "customer"}:
        return []
    mode = "contract" if role == "supplier" else "both"
    found = []
    seen = set()
    for raw in _raw_names(_plain(text), mode):
        party = accept_party(raw, registrants, names)
        if not party:
            continue
        key = party["name_normalised"]
        if not key or key in seen:
            continue
        seen.add(key)
        found.append(party["name"])
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


def _company_parties(client, cik, suppliers, customers, as_of, names, registrants, peers, group):
    start, end = search_window(as_of)
    sub_url = submissions_url(cik)
    _require_sec(sub_url)
    payload = client.json(sub_url)
    if not isinstance(payload, dict):
        raise EndpointError("submissions payload was not an object")
    texts = []
    for filing in select_filings(payload, suppliers=suppliers, customers=customers, start=start, end=end):
        if filing["form"] == "10-K":
            if not customers:
                continue
            mode = "concentration"
        else:
            mode = "contract"
        url = archive_document_url(cik, filing["accession"], filing["primary_document"])
        _require_sec(url)
        body, _content_type = client.get(url, accept="text/html,text/plain")
        texts.append((url, _plain(body), mode))
        if "1.01" in filing["items"]:
            for exhibit in _exhibit_names(client, cik, filing["accession"], filing["primary_document"]):
                exhibit_url = archive_document_url(cik, filing["accession"], exhibit)
                _require_sec(exhibit_url)
                exhibit_body, _content_type = client.get(exhibit_url, accept="text/html,text/plain")
                texts.append((exhibit_url, _plain(exhibit_body), "contract"))
    if suppliers:
        texts.extend((url, text, "contract") for url, text in _query_snippets(client, cik, CAPEX_QUERY, start, end))
    if customers:
        texts.extend((url, text, "both") for url, text in _query_snippets(client, cik, BACKLOG_QUERY, start, end))
    found = _collect(texts, suppliers, customers, names, registrants, group)
    _add_cross_hits(client, cik, peers, suppliers, customers, start, end, names, registrants, group, found)
    return found["rows"]


def _collect(texts, suppliers, customers, names, registrants, group):
    found = {"rows": [], "seen": set(), "counts": {"supplier": 0, "customer": 0}}
    for url, text, mode in texts:
        _absorb(found, text, mode, url, suppliers, customers, names, registrants, group)
    return found


def _absorb(found, text, mode, url, suppliers, customers, names, registrants, group):
    if mode == "concentration":
        roles = ["customer"] if customers else []
    elif mode == "both":
        roles = ["customer"] if customers else []
    else:
        roles = []
        if suppliers:
            roles.append("supplier")
        if customers:
            roles.append("customer")
    for raw in _raw_names(text, mode):
        party = accept_party(raw, registrants, names)
        if not party or _is_self_group(party, group):
            continue
        for role in roles:
            _add(found, role, party, url)


def _add_cross_hits(client, cik, peers, suppliers, customers, start, end, names, registrants, group, found):
    seen_queries = set()
    for peer in peers:
        key = normalise_name(peer["query"])
        if not key or key in seen_queries:
            continue
        seen_queries.add(key)
        if peer["lead_type"] == "orders_backlog" and suppliers:
            role = "supplier"
        elif peer["lead_type"] == "capex" and customers:
            role = "customer"
        else:
            continue
        page = _search_page(client, cik, _quoted(peer["query"]), start, end)
        url = _first_hit_url(cik, page)
        if not url:
            continue
        party = _party_from_peer(peer, registrants, names)
        if not party or _is_self_group(party, group):
            continue
        _add(found, role, party, url)


def _party_from_peer(peer, registrants, names):
    accepted = accept_party(peer["query"], registrants, names)
    if accepted and not is_common_heading(accepted["name"]):
        return accepted
    company = " ".join(str(peer.get("company") or peer.get("query") or "").split())
    if not company or is_common_heading(company):
        return None
    return _party(company, peer.get("ticker"))


def _add(found, role, party, url):
    if found["counts"][role] >= _PARTY_CAP:
        return
    key = (role, party["name_normalised"])
    if not party["name_normalised"] or key in found["seen"]:
        return
    found["seen"].add(key)
    found["counts"][role] += 1
    found["rows"].append(
        {
            "role": role,
            "name": party["name"],
            "name_normalised": party["name_normalised"],
            "ticker": party.get("ticker") or None,
            "source_url": url,
        }
    )


def _query_snippets(client, cik, query, start, end):
    return _snippets(cik, _search_page(client, cik, query, start, end))


def _search_page(client, cik, query, start, end):
    url = efts_url(cik, query, start, end)
    _require_sec(url)
    page = client.json(url)
    if not isinstance(page, dict):
        raise EndpointError("full-text payload was not an object")
    return page


def _raw_names(text, mode):
    found = []
    if mode in {"contract", "both"}:
        found.extend(_contract_names(text))
    if mode in {"concentration", "both"}:
        found.extend(_concentration_names(text))
    return found


def _contract_names(text):
    found = []
    for pattern in (_AGREEMENT, _CONTRACT):
        for match in pattern.finditer(text):
            found.append(match.group(1))
    for match in _BETWEEN.finditer(text):
        found.append(match.group(1))
        found.append(match.group(2))
    return found


def _concentration_names(text):
    found = []
    for pattern in (_ACCOUNTED, _LARGEST_AFTER, _LARGEST_BEFORE):
        for match in pattern.finditer(text):
            found.append(match.group(1))
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


def _first_hit_url(cik, page):
    hits = ((page.get("hits") or {}).get("hits")) or []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        url = _hit_url(cik, hit)
        if not url:
            continue
        _require_sec(url)
        return url
    return None


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


def _exhibit_names(client, cik, accession, primary):
    url = archive_document_url(cik, accession, "index.json")
    _require_sec(url)
    payload = client.json(url)
    if not isinstance(payload, dict):
        raise EndpointError("filing index payload was not an object")
    names = []
    for name in ex10_names(payload):
        if _same_doc(name, primary):
            continue
        names.append(name)
        if len(names) >= _EX10_LIMIT:
            break
    return names


def _peers_for(cik, group, groups, registrants):
    own = {(lead.ticker or "").upper() for lead in group if lead.ticker}
    peers = []
    seen = set()
    for other_cik, other_leads in groups.items():
        if other_cik == cik:
            continue
        for lead in other_leads:
            ticker = (lead.ticker or "").upper()
            if ticker and ticker in own:
                continue
            for label in _peer_labels(lead, registrants):
                key = (lead.lead_type, normalise_name(label))
                if key in seen:
                    continue
                seen.add(key)
                peers.append(
                    {
                        "query": label,
                        "company": lead.company or label,
                        "ticker": ticker,
                        "lead_type": lead.lead_type,
                    }
                )
    return peers


def _peer_labels(lead, registrants):
    labels = []
    company = " ".join(str(getattr(lead, "company", None) or "").split())
    ticker = str(getattr(lead, "ticker", None) or "").strip().upper()
    if company and not is_common_heading(company):
        labels.append(company)
    title = _title_for_ticker(registrants, ticker)
    if not title or is_common_heading(title):
        return _unique_labels(labels)
    title_key = normalise_name(title)
    if title_key and title_key not in {normalise_name(company), normalise_name(ticker)}:
        labels.append(title)
    return _unique_labels(labels)


def _unique_labels(labels):
    found = []
    seen = set()
    for label in labels:
        key = normalise_name(label)
        if not key or key in seen:
            continue
        seen.add(key)
        found.append(label)
    return found


def _title_for_ticker(registrants, ticker):
    if not registrants or not ticker:
        return ""
    hit = (registrants.get("by_ticker") or {}).get(ticker)
    if not hit:
        return ""
    return hit[0]


def _lookup_registrant(name, registrants):
    if not registrants:
        return None
    by_name = registrants.get("by_name") or {}
    key = normalise_name(_core_text(name) or name)
    if key in by_name:
        return by_name[key]
    stripped = normalise_name(" ".join(_stem_tokens(name)))
    if stripped and stripped in by_name:
        return by_name[stripped]
    tokens = (_core_text(name) or "").split()
    if len(tokens) == 1:
        ticker = _fold_token(tokens[0]).upper()
        hit = (registrants.get("by_ticker") or {}).get(ticker)
        if hit:
            return hit
    return None


def _party(name, ticker):
    ticker = str(ticker or "").strip().upper()
    return {
        "name": " ".join(str(name).split()),
        "name_normalised": normalise_name(name),
        "ticker": ticker or None,
    }


def _ticker_from_names(core, names):
    if not names:
        return None
    keys = [normalise_name(core), normalise_name(" ".join(_stem_tokens(core)))]
    for key in keys:
        if key and names.get(key):
            return names.get(key)
    return None


def _is_self_group(party, group):
    return any(_is_self(party, lead) for lead in group)


def _is_self(party, lead):
    lead_ticker = (getattr(lead, "ticker", None) or "").strip().upper()
    lead_company = getattr(lead, "company", None) or ""
    party_ticker = (party.get("ticker") or "").strip().upper()
    if lead_ticker and party_ticker == lead_ticker:
        return True
    party_key = party.get("name_normalised") or normalise_name(party.get("name"))
    company_key = normalise_name(lead_company)
    ticker_key = normalise_name(lead_ticker)
    if party_key and company_key and party_key == company_key:
        return True
    if party_key and ticker_key and party_key == ticker_key:
        return True
    stem = normalise_name(" ".join(_stem_tokens(party.get("name") or "")))
    if stem and company_key and stem == company_key:
        return True
    if stem and ticker_key and stem == ticker_key:
        return True
    return False


def _core_text(raw):
    tokens = [token for token in str(raw or "").split() if token]
    while tokens and _fold_token(tokens[0]) in _LEADING:
        tokens.pop(0)
    text = " ".join(tokens).strip(" ,;:\"'()[]")
    return " ".join(text.split())


def _stem_tokens(raw):
    tokens = (_core_text(raw) or "").split()
    if tokens and _is_suffix_token(tokens[-1]):
        tokens = tokens[:-1]
    return tokens


def _stem_is_common(raw):
    tokens = _stem_tokens(raw)
    if not tokens:
        return True
    return all(_fold_token(token) in _COMMON for token in tokens)


def _has_suffix(raw):
    tokens = (_core_text(raw) or "").split()
    return bool(tokens) and _is_suffix_token(tokens[-1])


def _is_suffix_token(token):
    folded = token.casefold().strip("\"'()[] ,;:")
    compact = folded.replace(".", "")
    if compact in _DOTTED_SUFFIXES:
        return "." in token
    return folded.rstrip(".") in _SUFFIXES


def _fold_token(token):
    return token.casefold().strip(".,;:\"'()[]")


def _quoted(text):
    return '"' + str(text).replace('"', "") + '"'


def _same_doc(left, right):
    return left.rsplit("/", 1)[-1].casefold() == right.rsplit("/", 1)[-1].casefold()


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
