"""Parse SEC Schedule 13D/13G and ASX Forms 603/604/605 into positions.

XML field names follow the EDGAR Schedule 13D and 13G schemas (primary_doc.xml):
13D uses reportingPersonInfo, percentOfClass, aggregateAmountOwned, dateOfEvent,
and item4/transactionPurpose. 13G uses coverPageHeaderReportingPersonDetails,
classPercent, reportingPersonBeneficiallyOwnedAggregateNumberOfShares,
eventDateRequiresFilingThisStatement, and item5/classOwnership5PercentOrLess.
Legacy SC 13D and SC 13G HTML/text is read from the cover-page labels.
"""

import html
import re
from xml.etree import ElementTree

from Scouts.accumulation.forms import canonical_form, sec_family
from Scouts.accumulation.model import Position, norm_acn, norm_cik

# Joint filers often report the same shares. A wider spread is treated as a conflict.
_STAKE_SPREAD = 1.0

_PCT_FIELDS = ("percentOfClass", "classPercent")
_SHARE_FIELDS = (
    "aggregateAmountOwned",
    "reportingPersonBeneficiallyOwnedAggregateNumberOfShares",
    "amountBeneficiallyOwned",
)
_PERSON_TAGS = ("reportingPersonInfo", "coverPageHeaderReportingPersonDetails")
_EVENT_TAGS = ("dateOfEvent", "eventDateRequiresFilingThisStatement")


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
        if node is elem:
            continue
        if local(node.tag).lower() in wanted:
            text = "".join(node.itertext()).strip()
            if text:
                return text
    return None


def parse_document(document, ticker_by_cik=None):
    """Return one or more positions. Parse failures become flagged positions."""
    ticker_by_cik = ticker_by_cik or {}
    if document.source == "sec":
        return _parse_sec(document, ticker_by_cik)
    if document.source == "asx":
        return _parse_asx(document)
    return [_blank(document, "conflicting_data", f"unknown source {document.source}")]


def _parse_sec(document, ticker_by_cik):
    body = (document.body or "").lstrip("\ufeff")
    if body.startswith("%PDF"):
        return [_blank(document, "unparseable_stake", "PDF filing; stake not extracted")]
    if "<edgarSubmission" in body[:8000] or body.lstrip().startswith("<?xml"):
        try:
            root = ElementTree.fromstring(body)
        except ElementTree.ParseError:
            return [_blank(document, "unparseable_stake", "XML parse error")]
        if local(root.tag).lower() == "edgarsubmission" or find_all(root, "edgarSubmission"):
            if local(root.tag).lower() != "edgarsubmission":
                root = find_all(root, "edgarSubmission")[0]
            return _parse_sec_xml(document, root, ticker_by_cik)
    return _parse_sec_text(document, _to_text(body), ticker_by_cik)


def _parse_sec_xml(document, root, ticker_by_cik):
    submission = first_text(root, "submissionType")
    form_type = canonical_form(submission) or canonical_form(document.form_type) or "UNKNOWN"
    family, amendment = sec_family(form_type)
    flags = []
    notes = []
    if submission and document.form_type:
        meta_family, meta_amend = sec_family(document.form_type)
        if meta_family and family and (meta_family != family or meta_amend != amendment):
            flags.append("conflicting_data")
            notes.append(
                f"index form {canonical_form(document.form_type)} disagrees with "
                f"submissionType {canonical_form(submission)}"
            )
    if family is None:
        flags.append("conflicting_data")
        notes.append(f"unrecognised SEC form type {form_type}")
        family = "unknown"

    issuer_nodes = find_all(root, "issuerInfo")
    issuer_scope = issuer_nodes[0] if issuer_nodes else root
    issuer_name = first_text(issuer_scope, "issuerName") or ""
    issuer_cik = norm_cik(first_text(issuer_scope, "issuerCIK", "issuerCik"))
    event_date = _us_date(first_text(root, *_EVENT_TAGS))
    if first_text(root, *_EVENT_TAGS) and event_date is None:
        flags.append("unparseable_date")
        notes.append("event date is not a US or ISO date")

    credentials = find_all(root, "filerCredentials")
    header_cik = norm_cik(first_text(credentials[0], "cik")) if credentials else None
    persons = []
    for tag in _PERSON_TAGS:
        persons.extend(find_all(root, tag))
    parsed_people = [_person_numbers(node) for node in persons]
    if not parsed_people:
        parsed_people = [_document_level_numbers(root)]

    stakes = [item["stake"] for item in parsed_people if item["stake"] is not None]
    if len(stakes) >= 2 and max(stakes) - min(stakes) > _STAKE_SPREAD:
        flags.append("conflicting_data")
        notes.append(f"reporting-person stakes disagree ({min(stakes)} to {max(stakes)})")

    primary = _pick_primary(parsed_people, header_cik)
    co_filers = [
        item["name"]
        for item in parsed_people
        if item is not primary and item["name"] and item["name"] != primary["name"]
    ]
    stake = primary["stake"]
    shares = primary["shares"]
    if stake is None and len(parsed_people) == 1:
        fallback = _document_level_numbers(root)
        stake = fallback["stake"]
        if shares is None:
            shares = fallback["shares"]

    item5 = first_text(root, "percentageOfClassSecurities")
    if stake is not None and item5:
        item5_pcts = [float(n) for n in re.findall(r"(\d+(?:\.\d+)?)\s*%", item5)]
        if item5_pcts and all(abs(pct - stake) > 0.2 for pct in item5_pcts):
            flags.append("conflicting_data")
            notes.append(f"Item 5 percent {item5_pcts} disagrees with cover stake {stake}")

    ceased = False
    exit_flag = (first_text(root, "classOwnership5PercentOrLess") or "").strip().upper()
    if exit_flag == "Y":
        ceased = True
        notes.append("Item 5 reports ownership of 5 percent or less")
        if stake is not None and stake > 5:
            flags.append("conflicting_data")
            notes.append(f"exit flag is set but stake is {stake}")

    purpose = None
    if family == "13D":
        purpose = first_text(root, "transactionPurpose")

    if stake is None and not ceased:
        flags.append("unparseable_stake")
        notes.append("no percentOfClass or classPercent")
    elif stake is not None and (stake < 0 or stake > 100):
        flags.append("conflicting_data")
        notes.append(f"stake {stake} is outside 0-100")

    ticker = ticker_by_cik.get(issuer_cik) if issuer_cik else None
    if not issuer_name and not issuer_cik:
        flags.append("conflicting_data")
        notes.append("issuer name and CIK missing")

    return [
        Position(
            source="sec",
            form_type=form_type,
            form_family=family,
            amendment=amendment,
            filer=primary["name"] or "",
            filer_id=primary["cik"] or header_cik,
            co_filers=co_filers,
            target_name=issuer_name,
            target_id=issuer_cik,
            ticker=ticker,
            stake_pct=stake,
            share_count=shares,
            event_date=event_date,
            filing_date=_iso(document.filing_date),
            purpose_text=purpose,
            document_url=document.url,
            accession=document.accession,
            ceased=ceased,
            flags=_uniq(flags),
            notes=notes,
        )
    ]


def _person_numbers(node):
    return {
        "name": first_text(node, "reportingPersonName") or "",
        "cik": norm_cik(first_text(node, "reportingPersonCIK", "reportingPersonCik")),
        "stake": _first_number(node, _PCT_FIELDS),
        "shares": _first_shares(node, _SHARE_FIELDS),
    }


def _document_level_numbers(root):
    """Item-level figures, used only when the cover page has a single person."""
    item4 = find_all(root, "item4")
    scope = item4[0] if item4 else root
    return {
        "name": first_text(root, "reportingPersonName") or "",
        "cik": None,
        "stake": _first_number(scope, _PCT_FIELDS),
        "shares": _first_shares(scope, _SHARE_FIELDS),
    }


def _pick_primary(people, header_cik):
    if header_cik:
        for person in people:
            if person["cik"] == header_cik:
                return person
    ranked = [person for person in people if person["stake"] is not None]
    if ranked:
        return max(ranked, key=lambda person: person["stake"])
    return people[0]


def _first_number(node, names):
    for name in names:
        raw = first_text(node, name)
        number = _strict_float(raw)
        if number is not None:
            return number
    return None


def _first_shares(node, names):
    for name in names:
        raw = first_text(node, name)
        shares = _strict_shares(raw)
        if shares is not None:
            return shares
    return None


def _parse_sec_text(document, text, ticker_by_cik):
    form_type = canonical_form(document.form_type) or "SC 13D"
    family, amendment = sec_family(form_type)
    flags = []
    notes = []
    if family is None:
        flags.append("conflicting_data")
        notes.append(f"unrecognised SEC form type {form_type}")
        family = "unknown"

    issuer = _line_value(text, r"\(Name of Issuer\)") or _line_value(text, r"Name of Issuer")
    filer = _line_value(text, r"Names of Reporting Persons")
    event_raw = _line_value(text, r"Date of Event Which Requires Filing of this Statement")
    event_date = _us_date(event_raw)
    if event_raw and event_date is None:
        flags.append("unparseable_date")
        notes.append("event date is not a US or ISO date")

    stake = _percent_of_class(text)
    shares = None
    share_match = re.search(
        r"aggregate amount beneficially owned[^\d]{0,60}(\d[\d,]*)",
        text,
        re.I,
    )
    if share_match:
        shares = _strict_shares(share_match.group(1))

    purpose = None
    if family == "13D":
        purpose_match = re.search(
            r"Item\s*4[\s.:]+Purpose of Transaction\s*(.*?)\s*Item\s*5[\s.:]",
            text,
            re.I | re.S,
        )
        if purpose_match:
            purpose = " ".join(purpose_match.group(1).split())

    if stake is None:
        flags.append("unparseable_stake")
        notes.append("cover page percent of class missing")
    elif stake < 0 or stake > 100:
        flags.append("conflicting_data")
        notes.append(f"stake {stake} is outside 0-100")
    if not issuer or not filer:
        flags.append("conflicting_data")
        notes.append("issuer or reporting person missing")

    cik_match = re.search(r"SUBJECT COMPANY[\s\S]{0,200}?CIK[:\s]+0*(\d+)", text, re.I)
    issuer_cik = norm_cik(cik_match.group(1)) if cik_match else None
    ticker = ticker_by_cik.get(issuer_cik) if issuer_cik else None
    return [
        Position(
            source="sec",
            form_type=form_type,
            form_family=family,
            amendment=amendment,
            filer=filer or "",
            target_name=issuer or "",
            target_id=issuer_cik,
            ticker=ticker,
            stake_pct=stake,
            share_count=shares,
            event_date=event_date,
            filing_date=_iso(document.filing_date),
            purpose_text=purpose,
            document_url=document.url,
            accession=document.accession,
            flags=_uniq(flags),
            notes=notes,
        )
    ]


def _parse_asx(document):
    body = document.body or ""
    if body.startswith("%PDF"):
        return [_blank(document, "unparseable_stake", "PDF notice; voting power not extracted")]
    text = _to_text(body)
    form = _asx_form_from_text(text)
    hinted = _asx_form_from_hint(document.form_type)
    flags = []
    notes = []
    if form and hinted and form != hinted:
        flags.append("conflicting_data")
        notes.append(f"announcement form {hinted} disagrees with notice text Form {form}")
    if form is None:
        form = hinted
    if form is None:
        flags.append("conflicting_data")
        notes.append("notice is not Form 603, 604, or 605")
        form = document.form_type or "unknown"

    company = _asx_company(text)
    holder = _asx_holder(text)
    acn_match = re.search(r"ACN/ARSN(?:/[A-Z]+)?\s*[:\-]?\s*([0-9][0-9 ]{4,})", text, re.I)
    target_id = norm_acn(acn_match.group(1)) if acn_match else None

    event_raw = None
    ceased = form == "605"
    if form == "603":
        event_raw = _after(text, r"became a substantial holder on")
    elif form == "604":
        event_raw = _after(text, r"change in the interests of the substantial holder on")
    elif form == "605":
        event_raw = _after(text, r"ceased to be a substantial holder on")
    event_date = _au_date(event_raw) if event_raw else None
    if event_raw and event_date is None:
        flags.append("unparseable_date")
        notes.append("event date is not an Australian or ISO date")

    stake, shares, previous = _asx_voting(text, form)
    if previous is not None:
        notes.append(f"previous voting power {previous}%")
    if stake is None and not ceased:
        flags.append("unparseable_stake")
        notes.append("voting power missing")
    elif stake is not None and (stake < 0 or stake > 100):
        flags.append("conflicting_data")
        notes.append(f"voting power {stake} is outside 0-100")
    if not company and not document.ticker:
        flags.append("conflicting_data")
        notes.append("target name and ticker missing")
    if not holder:
        flags.append("conflicting_data")
        notes.append("substantial holder name missing")

    return [
        Position(
            source="asx",
            form_type=form,
            form_family=form,
            amendment=form == "604",
            filer=holder or "",
            target_name=company or "",
            target_id=target_id,
            ticker=(document.ticker or "").upper() or None,
            stake_pct=stake,
            share_count=shares,
            event_date=event_date,
            filing_date=_iso(document.filing_date),
            purpose_text=None,
            document_url=document.url,
            accession=document.accession,
            ceased=ceased,
            flags=_uniq(flags),
            notes=notes,
        )
    ]


_FIELD_LABEL = re.compile(
    r"^(?:address|acn(?:/arsn(?:/[a-z]+)?)?(?:\s*\(if applicable\))?|name|"
    r"notice|company(?:\s+name(?:/scheme)?)?|details of substantial holder.*|"
    r"the holder\b.*|class of securities.*|previous notice|present notice|"
    r"person's votes|voting power.*)\s*$",
    re.I,
)


def _field_label(value):
    return _FIELD_LABEL.match((value or "").strip()) is not None


def _next_value(lines, start):
    for later in lines[start:]:
        later = later.strip()
        if not later or _field_label(later):
            continue
        return later
    return None


def _asx_company(text):
    labeled = _labeled(text, r"Company(?:/[^\n:]{0,80})?\s+name") or _labeled(text, r"Company name")
    if labeled and not _field_label(labeled):
        return labeled
    match = re.search(r"Company Name/Scheme[ \t]+(\S.*)", text, re.I)
    if match and not _field_label(match.group(1)):
        return match.group(1).strip()
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if re.search(r"Company Name/Scheme\s*$", line, re.I) or re.fullmatch(r"\s*Company name\s*:?\s*", line, re.I):
            found = _next_value(lines, index + 1)
            if found:
                return found
    return None


def _asx_holder(text):
    """The substantial holder, not the Address label that follows Name on the form."""
    match = re.search(
        r"Details of substantial holder\b(.*?)(?:\n\s*2\.\s|\n\s*There was a change|\n\s*The holder became)",
        text,
        re.I | re.S,
    )
    block = match.group(1) if match else text
    lines = block.splitlines()
    for index, line in enumerate(lines):
        named = re.fullmatch(r"\s*Name\s*[:\-]?\s*(.*)", line, re.I)
        if not named:
            continue
        value = named.group(1).strip()
        if value and not _field_label(value):
            return value
        found = _next_value(lines, index + 1)
        if found:
            return found
    return None


def _asx_form_from_hint(hinted):
    key = canonical_form(hinted)
    if key in {"603", "604", "605", "FORM 603", "FORM 604", "FORM 605"}:
        return key.split()[-1]
    return None


def _asx_form_from_text(text):
    numbered = re.search(r"\bForm\s*(603|604|605)\b", text, re.I)
    if numbered:
        return numbered.group(1)
    folded = text.casefold()
    if "ceasing to be a substantial holder" in folded or "ceased to be a substantial holder" in folded:
        return "605"
    if "change of interests of substantial holder" in folded or "change in substantial holding" in folded:
        return "604"
    if "initial substantial holder" in folded or "becoming a substantial holder" in folded:
        return "603"
    return None


def _asx_voting(text, form):
    """Colon-labelled notices and the ASIC table layout from pdftotext -layout.

    On a Form 604 the present column is the current stake. A table row keeps
    the previous percent and the present percent on one line, with the share
    counts beside them, so the numbers are not adjacent to the column labels.
    """
    if form == "604":
        return _asx_604_voting(text)
    if form == "605":
        stake = _voting_power(text)
        shares = _share_count(text, r"Person's votes")
        if stake is None or shares is None:
            row_stake, row_shares, _previous = _from_data_rows(_data_rows(text))
            stake = stake if stake is not None else row_stake
            shares = shares if shares is not None else row_shares
        return stake, shares, None
    stake = _voting_power(text)
    shares = _share_count(text, r"Number of securities")
    if stake is None or shares is None:
        row_stake, row_shares, _previous = _from_data_rows(_data_rows(text))
        stake = stake if stake is not None else row_stake
        shares = shares if shares is not None else row_shares
    return stake, shares, None


def _asx_604_voting(text):
    folded = text.casefold()
    previous_at = folded.find("previous notice")
    present_at = folded.find("present notice")
    previous = present_stake = present_shares = None
    if previous_at >= 0 and present_at > previous_at:
        previous = _voting_power(text[previous_at:present_at])
        present_stake = _voting_power(text[present_at:])
        present_shares = _share_count(text[present_at:], r"Person's votes")
        if previous is None or present_stake is None or present_shares is None:
            row_stake, row_shares, row_previous = _from_data_rows(_data_rows(text[previous_at:]))
            if previous is None:
                previous = row_previous
            if present_stake is None:
                present_stake = row_stake
            if present_shares is None:
                present_shares = row_shares
        return present_stake, present_shares, previous
    stake = _voting_power(text)
    shares = _share_count(text, r"Person's votes")
    if stake is None or shares is None:
        row_stake, row_shares, _previous = _from_data_rows(_data_rows(text))
        stake = stake if stake is not None else row_stake
        shares = shares if shares is not None else row_shares
    return stake, shares, None


def _data_rows(text):
    """Lines that carry both a voting-power percent and a share count."""
    rows = []
    for line in text.splitlines():
        percents = [float(item) for item in re.findall(r"(\d+(?:\.\d+)?)\s*(?:%|per\s*cent\b)", line, re.I)]
        shares = []
        for raw in re.findall(r"\d{1,3}(?:,\d{3})+|\d{5,}", line):
            parsed = _strict_shares(raw)
            if parsed:
                shares.append(parsed)
        if percents and shares:
            rows.append((percents, shares))
    return rows


def _from_data_rows(rows):
    """Return (present stake, present shares, previous stake) from table rows."""
    if not rows:
        return None, None, None
    if len(rows) == 1 and len(rows[0][0]) >= 2:
        percents, shares = rows[0]
        present_shares = shares[-1] if len(shares) >= 2 else (shares[0] if shares else None)
        return percents[-1], present_shares, percents[0]
    if len(rows) == 1:
        percents, shares = rows[0]
        return percents[-1], (shares[0] if shares else None), None
    present_pct, present_shares = rows[-1]
    previous = rows[0][0][0]
    return present_pct[-1], (present_shares[-1] if present_shares else None), previous


def _voting_power(text):
    match = re.search(
        r"voting power(?:\s*\(\d+\))?\s*[:\-]?\s*(\d+(?:\.\d+)?)\s*(?:%|per\s*cent\b)",
        text,
        re.I,
    )
    if not match:
        return None
    return float(match.group(1))


def _share_count(text, label):
    match = re.search(rf"{label}\s*[:\-]?\s*(\d[\d,]*)", text, re.I)
    if not match:
        return None
    return _strict_shares(match.group(1))


def _blank(document, flag, note):
    family, amendment = sec_family(document.form_type) if document.source == "sec" else (None, False)
    form_type = canonical_form(document.form_type) or "UNKNOWN"
    return Position(
        source=document.source,
        form_type=form_type or "UNKNOWN",
        form_family=family or (document.form_type or "unknown"),
        amendment=amendment,
        filer="",
        target_name="",
        ticker=(document.ticker or "").upper() or None,
        document_url=document.url,
        accession=document.accession,
        filing_date=_iso(document.filing_date),
        flags=[flag],
        notes=[note],
    )


def _to_text(body):
    if "<" in body and ">" in body:
        text = re.sub(r"(?is)<script\b.*?>.*?</script>", " ", body)
        text = re.sub(r"(?is)<style\b.*?>.*?</style>", " ", text)
        text = re.sub(r"(?i)<br\s*/?>", "\n", text)
        text = re.sub(r"(?i)</p>|</tr>|</h[1-6]>", "\n", text)
        text = re.sub(r"(?i)</t[dh]>", " ", text)
        text = re.sub(r"<[^>]+>", " ", text)
        body = html.unescape(text)
    lines = [" ".join(line.split()) for line in body.splitlines()]
    return "\n".join(line for line in lines if line)


def _percent_of_class(text):
    match = re.search(r"percent of class(.{0,180})", text, re.I | re.S)
    if not match:
        return None
    window = match.group(1)
    pct = re.search(r"(\d+(?:\.\d+)?)\s*%", window)
    if pct:
        return float(pct.group(1))
    for line in window.splitlines()[1:]:
        bare = re.search(r"(\d+(?:\.\d+)?)", line)
        if bare:
            return float(bare.group(1))
    return None


def _line_value(text, label):
    match = re.search(
        rf"(?:^|\n)\s*(?:\(\d+\)\s*)?{label}\s*[:\-]?\s*(?:\n\s*)?(.+)",
        text,
        re.I,
    )
    if match:
        return match.group(1).strip()
    return None


def _labeled(text, label):
    match = re.search(rf"(?:^|\n)\s*{label}\s*[:\-]\s*(.+)", text, re.I)
    if match:
        return match.group(1).strip()
    return None


def _after(text, label):
    match = re.search(rf"{label}\s*[:\-]?\s*(\d{{1,2}}/\d{{1,2}}/\d{{4}}|\d{{4}}-\d{{2}}-\d{{2}})", text, re.I)
    if match:
        return match.group(1)
    return None


def _strict_float(value):
    if value is None:
        return None
    text = value.strip().replace("%", "").replace(",", "").strip()
    if not re.fullmatch(r"\d+(?:\.\d+)?", text):
        return None
    return float(text)


def _strict_shares(value):
    number = _strict_float(value)
    if number is None or number < 0:
        return None
    if abs(number - round(number)) > 0.001:
        return None
    return int(round(number))


def _us_date(value):
    """MM/DD/YYYY as used by dateOfEvent, or ISO."""
    if not value:
        return None
    text = value.strip()
    iso = _iso(text)
    if iso:
        return iso
    match = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if not match:
        return None
    month, day, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
    return _ymd(year, month, day)


def _au_date(value):
    """DD/MM/YYYY as used on ASIC substantial holder forms, or ISO."""
    if not value:
        return None
    text = value.strip()
    iso = _iso(text)
    if iso:
        return iso
    match = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if not match:
        return None
    day, month, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
    return _ymd(year, month, day)


def _ymd(year, month, day):
    from datetime import date

    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _iso(value):
    if value and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        return value.strip()
    return None


def _uniq(items):
    seen = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen
