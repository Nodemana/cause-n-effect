"""Parse SEC Form 4 / 4/A ownership XML and ASX Appendix 3Y text.

Form 4 code P is the purchase code this scout keeps. The SEC definition of P
is an open-market or private purchase; the filing does not split those.
Appendix 3Y keeps on-market acquisitions only. Sales, grants, gifts, option
exercises, and off-market changes are left unmarked so the filter can drop them.
"""

import re
from datetime import date
from xml.etree import ElementTree

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from model import record

_ON_MARKET = re.compile(r"(?<![a-z])on[-\s]?market", re.I)
_OFF_MARKET = re.compile(r"(?<![a-z])off[-\s]?market", re.I)
_MONEY = re.compile(
    r"(?P<cur>A\$|US\$|AUD|USD|\$)?\s*(?P<num>\d[\d,]*(?:\.\d+)?)",
    re.I,
)


def parse_form4(body, source_url, filing_date, ticker=None):
    text = (body or "").lstrip("\ufeff").strip()
    if not text:
        return [_shell("sec", "4", source_url, filing_date, ticker, ["unparseable"], ["empty Form 4"])]
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return [_shell("sec", "4", source_url, filing_date, ticker, ["unparseable"], ["XML parse error"])]
    if local(root.tag).lower() != "ownershipdocument":
        found = find_all(root, "ownershipDocument")
        if not found:
            return [_shell("sec", "4", source_url, filing_date, ticker, ["unparseable"], ["not an ownership document"])]
        root = found[0]
    form = _form_name(first_text(root, "documentType"))
    owners = _owners(root)
    issuer_name = _direct_text(root, "issuer", "issuerName")
    issuer_ticker = (_direct_text(root, "issuer", "issuerTradingSymbol") or "").upper() or None
    flags = []
    notes = []
    if ticker and issuer_ticker and ticker.upper() != issuer_ticker:
        flags.append("conflicting_data")
        notes.append("filing ticker and issuer symbol differ")
    use_ticker = issuer_ticker or (ticker.upper() if ticker else None)
    if len(owners) > 1:
        flags.append("joint_filing")
        notes.append("more than one reporting owner")
    if not owners:
        return [_shell("sec", form, source_url, filing_date, use_ticker, ["unparseable"], ["no reporting owner"], company=issuer_name)]
    signed = _signature_date(root)
    filed = filing_date or signed
    rows = []
    transactions = find_all(root, "nonDerivativeTransaction")
    if not transactions:
        return []
    for owner in owners:
        for node in transactions:
            rows.append(_form4_row(node, owner, issuer_name, use_ticker, form, source_url, filed, flags, notes))
    return rows


def parse_3y(text, source_url, filing_date, ticker, company=None):
    raw = text or ""
    if not raw.strip():
        return [_shell("asx", "3Y", source_url, filing_date, ticker, ["unparseable"], ["empty Appendix 3Y"], company=company)]
    director, entity, position, parts = _3y_parts(raw)
    rows = []
    for part in parts:
        rows.append(_3y_row(part, director, entity or company, position, ticker, source_url, filing_date))
    return rows


def is_ceo_title(title):
    text = (title or "").casefold()
    if re.search(r"\bceo\b", text):
        return True
    return "chief executive" in text


def parse_date(value):
    text = (value or "").strip()
    if not text:
        return None
    iso = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    if iso:
        return _safe_date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
    local = re.match(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", text)
    if local:
        return _safe_date(int(local.group(3)), int(local.group(2)), int(local.group(1)))
    return None


def local(tag):
    if tag.startswith("{"):
        return tag.rsplit("}", 1)[-1]
    return tag


def find_all(elem, name):
    wanted = name.lower()
    return [node for node in elem.iter() if local(node.tag).lower() == wanted]


def first_text(elem, *names):
    wanted = {name.lower() for name in names}
    for node in elem.iter():
        if local(node.tag).lower() in wanted:
            text = "".join(node.itertext()).strip()
            if text:
                return text
    return None


def _form4_row(node, owner, company, ticker, form, source_url, filing_date, shared_flags, shared_notes):
    code = (_tag_value(node, "transactionCode")[0] or "").upper()
    acquired = (_tag_value(node, "transactionAcquiredDisposedCode")[0] or "").upper()
    shares_text, shares_ok = _tag_value(node, "transactionShares")
    price_text, price_ok = _tag_value(node, "transactionPricePerShare")
    when = parse_date(_tag_value(node, "transactionDate")[0])
    shares = _number(shares_text) if shares_ok else None
    price = _number(price_text) if price_ok else None
    flags = list(shared_flags)
    notes = list(shared_notes)
    if not owner.get("name"):
        flags.append("unparseable")
        notes.append("reporting owner name missing")
    if not when:
        flags.append("unparseable_date")
        notes.append("transaction date missing")
    if not code:
        flags.append("unparseable")
        notes.append("transaction code missing")
    elif code == "P" and acquired != "A":
        flags.append("conflicting_data" if acquired else "ambiguous_item")
        notes.append("code P is not marked acquired")
    if _swap(node):
        flags.append("ambiguous_item")
        notes.append("equity swap involved")
    known = {"P", "S", "A", "M", "G", "F", "D", "C", "J", "K", "U", "W", "I", "L", "X", "Z"}
    if code and code not in known:
        flags.append("ambiguous_item")
        notes.append(f"unrecognised transaction code {code}")
    open_market = (
        code == "P"
        and acquired == "A"
        and not flags
    )
    if open_market and (shares is None or shares <= 0 or price is None or price <= 0):
        flags.append("missing_value")
        notes.append("open-market purchase without shares and price")
        open_market = False
    value = None
    if shares is not None and price is not None and shares > 0 and price > 0:
        value = shares * price
    return record(
        insider=owner["name"],
        role=owner["role"],
        is_ceo=owner["is_ceo"],
        company=company,
        ticker=ticker,
        shares=shares,
        price=price,
        value=value,
        currency="USD",
        transaction_date=when,
        filing_date=filing_date,
        source_url=source_url,
        source="sec",
        form=form,
        transaction_code=code or None,
        open_market=open_market and "missing_value" not in flags and "unparseable_date" not in flags,
        flags=flags,
        notes=notes,
    )


def _3y_row(part, director, company, header_title, ticker, source_url, filing_date):
    flags = []
    notes = []
    if not director:
        flags.append("unparseable")
        notes.append("director name missing")
    when = parse_date(_label(part, "Date of change"))
    if not when:
        flags.append("unparseable_date")
        notes.append("date of change missing")
    acquired = _int_field(part, "Number acquired")
    disposed = _int_field(part, "Number disposed")
    nature = _label(part, "Nature of change") or ""
    consideration = _label(part, "Value/Consideration") or ""
    blob = f"{nature} {consideration}"
    on_market = bool(_ON_MARKET.search(blob))
    off_market = bool(_OFF_MARKET.search(blob))
    if on_market and off_market:
        flags.append("ambiguous_item")
        notes.append("nature says both on-market and off-market")
    elif not on_market and not off_market and acquired:
        flags.append("ambiguous_item")
        notes.append("acquisition without an on-market or off-market nature")
    money = _consideration(consideration)
    shares = acquired
    price = money["per_share"]
    value = money["total"]
    if price is not None and shares:
        derived = price * shares
        if value is None:
            value = derived
        elif abs(derived - value) > max(1.0, 0.01 * value):
            flags.append("conflicting_data")
            notes.append("per-share consideration does not match the total")
    elif value is not None and shares:
        price = value / shares
        notes.append("price is consideration divided by shares")
    open_market = on_market and not off_market and not flags and bool(shares and shares > 0)
    if open_market and (value is None or value <= 0):
        flags.append("missing_value")
        notes.append("on-market acquisition without a consideration")
        open_market = False
    if acquired and disposed:
        flags.append("ambiguous_item")
        notes.append("both acquired and disposed")
        open_market = False
    title = _label(part, "Position") or _label(part, "Officer title") or header_title or ""
    role = "director"
    if title:
        role = f"{title}; director"
    return record(
        insider=director,
        role=role,
        is_ceo=is_ceo_title(title),
        company=company,
        ticker=(ticker or "").upper() or None,
        shares=shares,
        price=price,
        value=value,
        currency=money["currency"],
        transaction_date=when,
        filing_date=filing_date,
        source_url=source_url,
        source="asx",
        form="3Y",
        transaction_code="on-market" if on_market and not off_market else ("off-market" if off_market else None),
        open_market=open_market,
        flags=flags,
        notes=notes,
    )


def _owners(root):
    owners = []
    for node in _children_named(root, "reportingOwner"):
        name = _direct_text(node, "reportingOwnerId", "rptOwnerName")
        relationship = _child(node, "reportingOwnerRelationship")
        title = ""
        parts = []
        if relationship is not None:
            title = (_tag_value(relationship, "officerTitle")[0] or "").strip()
            if _flag(relationship, "isOfficer"):
                parts.append(title or "officer")
            if _flag(relationship, "isDirector"):
                parts.append("director")
            if _flag(relationship, "isTenPercentOwner"):
                parts.append("10% owner")
        owners.append(
            {
                "name": name,
                "role": "; ".join(part for part in parts if part),
                "is_ceo": is_ceo_title(title),
            }
        )
    return owners


def _form_name(value):
    text = (value or "4").upper().replace(" ", "")
    if text in {"4", "4/A"}:
        return text
    return text or "4"


def _signature_date(root):
    for node in find_all(root, "signatureDate"):
        parsed = parse_date("".join(node.itertext()))
        if parsed:
            return parsed
    return None


def _swap(node):
    for item in find_all(node, "equitySwapInvolved"):
        text = "".join(item.itertext()).strip().casefold()
        if text in {"1", "true", "yes", "y"}:
            return True
    return False


def _flag(scope, name):
    text, ok = _tag_value(scope, name)
    return ok and (text or "").strip().casefold() in {"1", "true", "yes", "y"}


def _tag_value(scope, name):
    """Return (text, present). A footnote with no value is present but empty."""
    nodes = [node for node in scope.iter() if local(node.tag).lower() == name.lower()]
    if not nodes:
        return None, False
    node = nodes[0]
    for child in list(node):
        if local(child.tag).lower() == "value":
            text = "".join(child.itertext()).strip()
            return (text or None), True
    if any(local(child.tag).lower() == "footnoteid" for child in node.iter()):
        return None, False
    text = "".join(node.itertext()).strip()
    return (text or None), bool(text)


def _direct_text(scope, parent_name, child_name):
    parent = _child(scope, parent_name)
    if parent is None:
        return None
    text, ok = _tag_value(parent, child_name)
    if ok and text:
        return text
    node = _child(parent, child_name)
    if node is None:
        return None
    value = "".join(node.itertext()).strip()
    return value or None


def _child(scope, name):
    wanted = name.lower()
    for node in list(scope):
        if local(node.tag).lower() == wanted:
            return node
    return None


def _children_named(scope, name):
    wanted = name.lower()
    return [node for node in list(scope) if local(node.tag).lower() == wanted]


def _3y_parts(text):
    chunks = re.split(r"(?=Date of change\b)", text, flags=re.I)
    header = chunks[0]
    director = _label(header, "Name of Director") or _label(text, "Name of Director")
    entity = _label(header, "Name of entity") or _label(header, "Name of Entity")
    position = _label(header, "Position") or _label(text, "Position") or _label(header, "Officer title") or ""
    bodies = [chunk for chunk in chunks[1:] if chunk.strip()] or [text]
    return director, entity, position, bodies


def _label(text, label):
    lines = text.splitlines()
    pattern = re.compile(rf"^{re.escape(label)}\s*[:.]?\s*(.*)$", re.I)
    for index, line in enumerate(lines):
        match = pattern.match(line.strip())
        if not match:
            continue
        value = match.group(1).strip()
        if value:
            return value
        for follow in lines[index + 1:]:
            follow = follow.strip()
            if not follow:
                continue
            if re.match(r"^[A-Za-z].{0,60}:\s*", follow):
                return None
            return follow
    return None


def _int_field(text, label):
    raw = _label(text, label)
    if raw is None:
        return None
    if raw.strip().casefold() in {"", "-", "nil", "n/a", "none"}:
        return None
    match = re.search(r"\d[\d,]*", raw)
    if not match:
        return None
    return int(match.group(0).replace(",", ""))


def _consideration(raw):
    currency = "AUD"
    folded = (raw or "").casefold()
    if "usd" in folded or "us$" in folded:
        currency = "USD"
    per_share = bool(re.search(r"per share", raw or "", re.I))
    match = _MONEY.search(raw or "")
    amount = None
    if match:
        amount = float(match.group("num").replace(",", ""))
        token = (match.group("cur") or "").casefold()
        if token in {"usd", "us$"}:
            currency = "USD"
        elif token in {"aud", "a$"}:
            currency = "AUD"
    if per_share:
        return {"per_share": amount, "total": None, "currency": currency}
    return {"per_share": None, "total": amount, "currency": currency}


def _number(value):
    if value is None:
        return None
    text = str(value).replace(",", "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _safe_date(year, month, day):
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _shell(source, form, source_url, filing_date, ticker, flags, notes, company=None):
    return record(
        company=company,
        ticker=(ticker or "").upper() or None,
        currency="USD" if source == "sec" else "AUD",
        filing_date=filing_date,
        source_url=source_url,
        source=source,
        form=form,
        flags=flags,
        notes=notes,
    )
