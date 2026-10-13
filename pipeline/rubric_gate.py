"""Code gate for the current fixed rubric of a lead type.

Every criterion is a number or a pass/fail fact. A candidate proceeds only
when every criterion passes. A failed candidate is stored later with status
filtered. A missing or stale official price is status needs_price. This
module does not guess a market cap and does not call a model.

Macro leads are not gated. A capex chain or a smart-money lead is gated on
each component's own lead type before the components are combined.
"""

import re
from datetime import date
from urllib.parse import urlsplit

from pipeline.load import ORDER_TYPES, filing_date
from pipeline.marketcap import resolve_market_cap
from pipeline.prefilter import _stake_pct

_PRICE_CRITERIA = {"min_market_cap", "raise_vs_company"}
_OFFICIAL_TYPES = {"filing", "company_release", "asx_announcement", "xbrl_company_facts"}
_NOT_OPEN_MARKET = re.compile(
    r"("
    r"\b(grant|gift|option exercise|exercise of options?|awarded)\b"
    r"|directors?'\s+fees?"
    r"|director'?s?\s+fees?"
    r"|salary[\s-]sacrifice"
    r"|fee[\s-]plan"
    r"|in lieu of"
    r"|as part of director"
    r"|\bshare\s+plan\b"
    r"|\brights\s+plan\b"
    r"|\bequity\s+plan\b"
    r"|\bplan\b"
    r")",
    re.IGNORECASE,
)
_CEASING = re.compile(r"\bceas", re.IGNORECASE)
_GENERAL_PROCEEDS = re.compile(r"general corporate purposes", re.IGNORECASE)
_SPECIFIC_PROCEEDS = re.compile(
    r"\b(spend\w*|capex|acquisition|acquir\w*|project|construction|expansion|development|plant|facility|bond|loan)\b",
    re.IGNORECASE,
)
_DEMAND = re.compile(r"\b(backlog|order)\b", re.IGNORECASE)


class Outcome:
    def __init__(self, status, results):
        self.status = status
        self.results = results


def evaluate(conn, figure, lead_type, now=None):
    """Return an Outcome. status is pass, filtered, or needs_price."""
    if lead_type == "macro":
        return Outcome("pass", [])
    rubric = _current_rubric(conn, lead_type)
    if rubric is None:
        return Outcome("filtered", [])
    cap, cap_note = resolve_market_cap(conn, figure.ticker, now=now)
    figure.market_cap = cap
    figure.market_cap_unverified = cap is None
    results = []
    for criterion in _criteria(conn, rubric["rubric_id"]):
        results.append(_check(figure, criterion, rubric, cap, cap_note, now))
    failed = [item for item in results if not item["passed"] and not item["needs_price"]]
    waiting = [item for item in results if item["needs_price"]]
    if failed:
        return Outcome("filtered", results)
    if waiting:
        return Outcome("needs_price", results)
    return Outcome("pass", results)


def failing_names(results):
    return [item["name"] for item in results if not item["passed"] and not item["needs_price"]]


def _current_rubric(conn, lead_type):
    return conn.execute(
        """
        SELECT rubric_id, version, lead_type
        FROM rubrics
        WHERE lead_type = ? AND fixed = 1
        ORDER BY version DESC
        LIMIT 1
        """,
        (lead_type,),
    ).fetchone()


def _criteria(conn, rubric_id):
    rows = conn.execute(
        """
        SELECT criterion_id, name, description, threshold_value, threshold_unit, sort_order
        FROM rubric_criteria
        WHERE rubric_id = ?
        ORDER BY sort_order
        """,
        (rubric_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _check(figure, criterion, rubric, cap, cap_note, now):
    name = criterion["name"]
    checker = _CHECKERS.get(name, _unknown)
    passed, measured, evidence, needs_price = checker(figure, criterion, cap, cap_note, now)
    return {
        "criterion_id": criterion["criterion_id"],
        "rubric_id": rubric["rubric_id"],
        "rubric_version": rubric["version"],
        "name": name,
        "passed": bool(passed) and not needs_price,
        "measured_value": measured,
        "evidence": evidence,
        "needs_price": bool(needs_price),
    }


def _unknown(figure, criterion, cap, cap_note, now):
    _ = (figure, cap, cap_note, now)
    return False, "", f"no code check for {criterion['name']}", False


def _min_market_cap(figure, criterion, cap, cap_note, now):
    _ = (figure, now)
    threshold = criterion["threshold_value"]
    unit = criterion["threshold_unit"] or "USD"
    if cap is None:
        return False, "", "price missing or stale", True
    currency = "AUD" if "AUD" in (cap_note or "") else unit
    measured = f"{cap:g} {currency}"
    if threshold is not None and cap < float(threshold):
        return (
            False,
            measured,
            f"{measured} is below {threshold:g} {unit}",
            False,
        )
    return True, measured, cap_note or measured, False


def _raise_vs_company(figure, criterion, cap, cap_note, now):
    _ = (cap_note, now)
    threshold = criterion["threshold_value"]
    if cap is None:
        return False, "", "price missing or stale", True
    if figure.value is None or threshold is None:
        return False, "", "raise amount missing", False
    pct = 100.0 * float(figure.value) / float(cap)
    measured = f"{pct:.4g} percent"
    if pct < float(threshold):
        return False, measured, f"{measured} of market cap is below {threshold:g}", False
    return True, measured, f"{measured} of market cap clears {threshold:g}", False


def _growth(label):
    def check(figure, criterion, cap, cap_note, now):
        _ = (cap, cap_note, now)
        threshold = criterion["threshold_value"]
        if not getattr(figure, "prior_comparison", False):
            return False, "", f"{label}: no prior comparison in the figures table", False
        growth = getattr(figure, "growth_pct", None)
        if growth is None or threshold is None:
            return False, "", f"{label}: no prior comparison in the figures table", False
        measured = f"{float(growth):.4g} percent"
        text = getattr(figure, "growth_text", None) or measured
        if float(growth) < float(threshold):
            return False, measured, f"below {label} threshold {threshold:g} percent", False
        return True, measured, text, False

    return check


def _official_source(figure, criterion, cap, cap_note, now):
    _ = (criterion, cap, cap_note, now)
    source_type = figure.source_type or ""
    if source_type in _OFFICIAL_TYPES or _official_host(figure.source_url):
        measured = source_type or _host(figure.source_url)
        return True, measured, "official filing or company release", False
    return False, source_type, "source is not a filing or a company release", False


def _specific_proceeds(figure, criterion, cap, cap_note, now):
    _ = (criterion, cap, cap_note, now)
    blob = _blob(figure)
    if _GENERAL_PROCEEDS.search(blob):
        return False, "general corporate purposes", "proceeds are general corporate purposes", False
    if _SPECIFIC_PROCEEDS.search(blob):
        return True, "named use", "proceeds name spending, an acquisition, or a project", False
    return False, "", "proceeds do not name spending, an acquisition, or a project", False


def _insider_buy_dollars(figure, criterion, cap, cap_note, now):
    _ = (cap, cap_note, now)
    threshold = criterion["threshold_value"]
    amount = figure.value
    if amount is None or threshold is None or float(amount) < float(threshold):
        measured = "" if amount is None else f"{float(amount):g}"
        return False, measured, f"below insider_buy_dollars threshold {threshold:g}", False
    measured = f"{float(amount):g}"
    return True, measured, f"{measured} clears {threshold:g}", False


def _open_market(figure, criterion, cap, cap_note, now):
    _ = (criterion, cap, cap_note, now)
    blob = _blob(figure)
    if _NOT_OPEN_MARKET.search(blob):
        return False, "not open market", "not a discretionary open-market buy", False
    return True, "open market", "open-market purchase", False


def _min_stake_pct(figure, criterion, cap, cap_note, now):
    _ = (cap, cap_note, now)
    threshold = criterion["threshold_value"]
    stake = _stake_pct(figure)
    if stake is None:
        return False, "", "stake percent missing", False
    measured = f"{float(stake):g}"
    if threshold is not None and float(stake) < float(threshold):
        return False, measured, f"below min_stake_pct threshold {threshold:g}", False
    return True, measured, f"{measured} percent clears {threshold:g}", False


def _recent_window(figure, criterion, cap, cap_note, now):
    _ = (cap, cap_note)
    threshold = criterion["threshold_value"]
    filed = filing_date(figure)
    anchor = _anchor(now)
    if not filed or anchor is None or threshold is None:
        return False, "", "filing date missing", False
    try:
        filed_on = date.fromisoformat(filed)
    except ValueError:
        return False, "", "filing date missing", False
    age = (anchor - filed_on).days
    measured = f"{age} days"
    if age > float(threshold):
        return False, measured, f"filing date is outside the {threshold:g} day window", False
    return True, measured, f"filing date is inside the {threshold:g} day window", False


def _not_ceasing(figure, criterion, cap, cap_note, now):
    _ = (criterion, cap, cap_note, now)
    if _CEASING.search(_blob(figure)):
        return False, "ceasing", "notice ceases a stake", False
    return True, "open or change", "notice opens or changes a stake", False


def _disclosed_demand(figure, criterion, cap, cap_note, now):
    _ = (criterion, cap, cap_note, now)
    if figure.figure_type in ORDER_TYPES or _DEMAND.search(_blob(figure)):
        return True, figure.figure_type or "order", "disclosed backlog or a named order", False
    return False, figure.figure_type or "", "not disclosed backlog or a named order", False


_CHECKERS = {
    "min_market_cap": _min_market_cap,
    "capex_growth_yoy": _growth("capex_growth_yoy"),
    "official_source": _official_source,
    "backlog_qoq_growth": _growth("backlog_qoq_growth"),
    "disclosed_demand": _disclosed_demand,
    "raise_vs_company": _raise_vs_company,
    "specific_proceeds": _specific_proceeds,
    "insider_buy_dollars": _insider_buy_dollars,
    "open_market": _open_market,
    "min_stake_pct": _min_stake_pct,
    "recent_window": _recent_window,
    "not_ceasing": _not_ceasing,
}


def _blob(figure):
    return " ".join(
        part
        for part in (
            figure.value_text,
            figure.instrument,
            figure.figure_type,
            figure.period,
        )
        if part
    )


def _anchor(now):
    if now is None:
        return None
    try:
        return date.fromisoformat(str(now)[:10])
    except ValueError:
        return None


def _official_host(url):
    host = _host(url)
    if host == "sec.gov" or host.endswith(".sec.gov"):
        return True
    if host == "asx.com.au" or host.endswith(".asx.com.au"):
        return True
    if host == "asx.api.markitdigital.com":
        return True
    return False


def _host(url):
    return (urlsplit(url or "").hostname or "").lower().rstrip(".")
