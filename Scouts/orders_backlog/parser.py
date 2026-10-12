"""Parse company-facts JSON and release text into one record shape.

No transcripts. No deferred-revenue or contract-liability proxies.
"""

import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_REPO = _ROOT.parents[1]
for _entry in (str(_REPO), str(_ROOT)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from model import archive_index_url, record
from shared.store import log_duplicate, prefer_fact
from tags import (
    ACTIVE_BACKLOG_TAGS,
    ACTIVE_INTAKE_TAGS,
    PROXY_TAGS,
    STALE_BACKLOG_TAGS,
)

_MONEY = re.compile(
    r"(?P<cur>A\$|US\$|USD|AUD|EUR|GBP|€|£|\$)\s*"
    r"(?P<num>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<scale>billion|million|thousand|bn|mn|m\b)?",
    re.I,
)
_SCALE = {
    "billion": 1_000_000_000,
    "bn": 1_000_000_000,
    "million": 1_000_000,
    "mn": 1_000_000,
    "m": 1_000_000,
    "thousand": 1_000,
}
_CURRENCY = {
    "$": "USD",
    "us$": "USD",
    "usd": "USD",
    "a$": "AUD",
    "aud": "AUD",
    "eur": "EUR",
    "€": "EUR",
    "gbp": "GBP",
    "£": "GBP",
}
_UNNAMED = re.compile(
    r"\b(undisclosed|unnamed|unidentified|confidential)\b|\bnot disclosed\b|\ba customer\b",
    re.I,
)
_TRANSCRIPT = re.compile(
    r"(?im)^operator\s*:|earnings call transcript|question-and-answer session"
)


def parse_company_facts(payload, company, source_side):
    """Turn a companyfacts JSON document into records for one universe entry."""
    facts = (payload or {}).get("facts") or {}
    active, stale = _tag_sets(company, source_side)
    by_key = {}
    for _namespace, concepts in facts.items():
        if not isinstance(concepts, dict):
            continue
        for concept, body in concepts.items():
            if concept in PROXY_TAGS:
                continue
            kind = None
            if concept in active:
                kind = "active"
            elif concept in stale:
                kind = "stale"
            if kind is None:
                continue
            units = (body or {}).get("units") or {}
            for currency, points in units.items():
                if not _is_currency(currency):
                    continue
                for point in points or []:
                    end = point.get("end")
                    key = (concept, end, currency)
                    by_key.setdefault(key, []).append((kind, point))

    active_concepts = {concept for (concept, _end, _cur), rows in by_key.items() if any(k == "active" for k, _ in rows)}
    stale_seen = any(kind == "stale" for rows in by_key.values() for kind, _ in rows)
    rows = []
    for (concept, end, currency), points in sorted(by_key.items()):
        kind = "stale" if points[0][0] == "stale" else "active"
        values = {point.get("val") for _kind, point in points}
        point = _choose_point(points, company, concept, end, currency)
        flags = []
        notes = []
        if len(values) > 1:
            flags.append("conflicting_value")
            notes.append(f"{concept} has more than one value for period {end}")
        if kind == "stale":
            flags.append("stale_tag")
            notes.append(f"{concept} is a stale backlog tag")
            if active_concepts:
                flags.append("tag_switch")
                notes.append("an active tag is also present for this company")
        elif stale_seen and concept in active_concepts:
            notes.append("stale tags also exist for this company; this row uses the active tag")
        if concept not in notes:
            notes.append(concept)
        value = point.get("val")
        if value is None:
            flags.append("missing_value")
        disclosure = "order_intake" if source_side == "supplier_order" else "backlog"
        timing = _fiscal_label(point)
        rows.append(
            record(
                source_side=source_side,
                company=company["name"],
                ticker=company["ticker"],
                counterparty=None,
                equipment_or_period=end,
                value=value,
                currency=currency,
                timing=timing,
                source_url=archive_index_url(company.get("cik") or "0", point.get("accn") or ""),
                filing_date=point.get("filed"),
                source_type="xbrl_company_facts",
                disclosure=disclosure,
                flags=flags,
                notes=notes,
            )
        )
    return rows


def parse_release(text, company, source_side, source_type, source_url, filing_date):
    """Parse an 8-K exhibit, ASX announcement, or IR release."""
    if not text or not text.strip():
        return [
            record(
                source_side=source_side,
                company=company["name"],
                ticker=company.get("ticker"),
                source_url=source_url,
                filing_date=filing_date,
                source_type=source_type,
                disclosure="backlog" if source_side == "customer_backlog" else "order",
                flags=["missing_value"],
                notes=["empty document"],
            )
        ]
    if _TRANSCRIPT.search(text):
        return [
            record(
                source_side=source_side,
                company=company["name"],
                ticker=company.get("ticker"),
                source_url=source_url,
                filing_date=filing_date,
                source_type=source_type,
                disclosure="backlog" if source_side == "customer_backlog" else "order",
                flags=["skipped_transcript"],
                notes=["earnings-call transcript; not parsed"],
            )
        ]
    if source_side == "customer_backlog":
        return _backlog_text(text, company, source_type, source_url, filing_date)
    return _supplier_text(text, company, source_type, source_url, filing_date)


def _backlog_text(text, company, source_type, source_url, filing_date):
    rows = []
    for sentence in _sentences(text):
        folded = sentence.casefold()
        if "backlog" not in folded:
            continue
        if "deferred revenue" in folded or "contract liabilit" in folded:
            rows.append(_shell(company, "customer_backlog", "backlog", source_type, source_url, filing_date,
                               ["ambiguous_value"], ["sentence mixes backlog with a deferred-revenue proxy"]))
            continue
        amounts = _moneys(sentence)
        if not amounts:
            rows.append(_shell(company, "customer_backlog", "backlog", source_type, source_url, filing_date,
                               ["missing_value"], ["backlog mentioned without a value"],
                               equipment_or_period=_period(sentence)))
            continue
        if len(amounts) > 1:
            rows.append(_shell(company, "customer_backlog", "backlog", source_type, source_url, filing_date,
                               ["ambiguous_value"], ["more than one amount in the backlog sentence"],
                               equipment_or_period=_period(sentence)))
            continue
        amount = amounts[0]
        rows.append(
            record(
                source_side="customer_backlog",
                company=company["name"],
                ticker=company.get("ticker"),
                equipment_or_period=_period(sentence),
                value=amount["value"],
                currency=amount["currency"],
                timing=_period(sentence),
                source_url=source_url,
                filing_date=filing_date,
                source_type=source_type,
                disclosure="backlog",
                notes=["release text"],
            )
        )
    return rows


def _supplier_text(text, company, source_type, source_url, filing_date):
    rows = []
    equipment_words = [item.casefold() for item in company.get("equipment") or []]
    for sentence in _sentences(text):
        folded = sentence.casefold()
        intake = "order intake" in folded or "orders received" in folded
        orderish = intake or any(word in folded for word in ("order", "contract", "awarded"))
        if not orderish and not any(word in folded for word in equipment_words):
            continue
        if not orderish:
            continue
        amounts = _moneys(sentence)
        equipment = _equipment(sentence, company)
        period = _period(sentence)
        capacity = _capacity(sentence)
        timing = _delivery(sentence) or period
        if intake:
            if not amounts:
                rows.append(_shell(company, "supplier_order", "order_intake", source_type, source_url, filing_date,
                                   ["missing_value"], ["order intake without a value"],
                                   equipment_or_period=period, timing=timing))
                continue
            if len(amounts) > 1:
                rows.append(_shell(company, "supplier_order", "order_intake", source_type, source_url, filing_date,
                                   ["ambiguous_value"], ["more than one order-intake amount"],
                                   equipment_or_period=period))
                continue
            amount = amounts[0]
            rows.append(
                record(
                    source_side="supplier_order",
                    company=company["name"],
                    ticker=company.get("ticker"),
                    counterparty=None,
                    equipment_or_period=period or equipment,
                    value=amount["value"],
                    currency=amount["currency"],
                    timing=timing,
                    capacity=capacity,
                    source_url=source_url,
                    filing_date=filing_date,
                    source_type=source_type,
                    disclosure="order_intake",
                    notes=["aggregate order intake"],
                )
            )
            continue
        counterparty, unnamed = _counterparty(sentence)
        flags = []
        notes = []
        value = currency = None
        if not amounts:
            flags.append("missing_value")
            notes.append("order announced without a value")
        elif len(amounts) > 1:
            flags.append("ambiguous_value")
            notes.append("more than one amount in the order sentence")
        else:
            value = amounts[0]["value"]
            currency = amounts[0]["currency"]
        if unnamed or not counterparty:
            flags.append("unnamed_customer")
            notes.append("customer not named")
        rows.append(
            record(
                source_side="supplier_order",
                company=company["name"],
                ticker=company.get("ticker"),
                counterparty=None if unnamed else counterparty,
                equipment_or_period=equipment,
                value=value,
                currency=currency,
                timing=timing,
                capacity=capacity,
                source_url=source_url,
                filing_date=filing_date,
                source_type=source_type,
                disclosure="order",
                flags=flags,
                notes=notes,
            )
        )
    return rows


def _choose_point(points, company, concept, end, currency):
    """Keep the single-quarter value, then the most recent filing."""
    chosen = points[0][1]
    for _kind, point in points[1:]:
        if prefer_fact(point, chosen):
            log_duplicate(
                _fact_row(company, concept, end, currency, chosen),
                _fact_row(company, concept, end, currency, point),
            )
            chosen = point
        else:
            log_duplicate(
                _fact_row(company, concept, end, currency, point),
                _fact_row(company, concept, end, currency, chosen),
            )
    return chosen


def _fact_row(company, concept, end, currency, point):
    return {
        "ticker": company.get("ticker") or "",
        "figure_type": concept,
        "instrument": "",
        "period": end or "",
        "value": point.get("val"),
        "currency": currency,
        "filed": point.get("filed"),
        "frame": point.get("frame"),
        "fp": point.get("fp"),
        "start": point.get("start"),
        "end": point.get("end") or end,
    }


def _tag_sets(company, source_side):
    if source_side == "customer_backlog":
        active = set(ACTIVE_BACKLOG_TAGS) | set(company.get("backlog_tags") or [])
        stale = set(STALE_BACKLOG_TAGS) | set(company.get("stale_tags") or [])
    else:
        active = set(ACTIVE_INTAKE_TAGS) | set(company.get("intake_tags") or [])
        stale = set()
    stale -= active
    return active, stale


def _is_currency(unit):
    return bool(re.fullmatch(r"[A-Z]{3}", unit or ""))


def _fiscal_label(point):
    fp = point.get("fp")
    fy = point.get("fy")
    if fp and fy:
        return f"{fp} FY{fy}"
    return None


def _sentences(text):
    cleaned = re.sub(r"<[^>]+>", " ", text)
    parts = re.split(r"(?<=[.!?])\s+|\n+", cleaned)
    return [part.strip() for part in parts if part.strip()]


def _moneys(sentence):
    found = []
    for match in _MONEY.finditer(sentence):
        scale = _SCALE.get((match.group("scale") or "").casefold(), 1)
        number = float(match.group("num").replace(",", ""))
        found.append(
            {
                "value": number * scale,
                "currency": _CURRENCY[(match.group("cur") or "").casefold()],
            }
        )
    return found


def _period(sentence):
    match = re.search(
        r"(?:quarter|period|year)\s+ended\s+([A-Z][a-z]+ \d{1,2}, \d{4}|\d{4}-\d{2}-\d{2})",
        sentence,
        re.I,
    )
    if not match:
        return None
    raw = match.group(1)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return raw
    from datetime import datetime

    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return raw


def _delivery(sentence):
    match = re.search(r"deliver(?:y|ed)?\s+(?:in\s+)?(\d{4}|[A-Z][a-z]+ \d{4})", sentence, re.I)
    if match:
        return "delivery " + match.group(1)
    return None


def _capacity(sentence):
    match = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*(MW|MVA)\b", sentence, re.I)
    if not match:
        return None
    return f"{match.group(1)} {match.group(2).upper()}"


def _equipment(sentence, company):
    folded = sentence.casefold()
    for word in company.get("equipment") or []:
        if word.casefold() in folded:
            return word
    for word in ("turbine", "transformer", "switchgear"):
        if word in folded:
            return word if word.endswith("s") else word + "s" if word == "turbine" else word
    if "turbine" in folded:
        return "turbines"
    return None


def _counterparty(sentence):
    if _UNNAMED.search(sentence):
        return None, True
    patterns = (
        r"\bfrom\s+([A-Z][\w&.'-]*(?:\s+[A-Z][\w&.'-]*){0,4})",
        r"\bawarded\b(?:\s+a\s+\w+){0,4}\s+by\s+([A-Z][\w&.'-]*(?:\s+[A-Z][\w&.'-]*){0,4})",
        r"\border with\s+([A-Z][\w&.'-]*(?:\s+[A-Z][\w&.'-]*){0,4})",
    )
    for pattern in patterns:
        match = re.search(pattern, sentence)
        if match:
            name = re.split(r"\s+\b(?:for|valued|worth|to supply)\b", match.group(1))[0].strip(" ,.")
            if name:
                return name, False
    return None, True


def _shell(company, source_side, disclosure, source_type, source_url, filing_date, flags, notes, equipment_or_period=None, timing=None):
    return record(
        source_side=source_side,
        company=company["name"],
        ticker=company.get("ticker"),
        equipment_or_period=equipment_or_period,
        timing=timing,
        source_url=source_url,
        filing_date=filing_date,
        source_type=source_type,
        disclosure=disclosure,
        flags=flags,
        notes=notes,
    )
