"""Deterministic fixture responder for tests.

Live `python -m pipeline run` does not import this module and does not call
a model. Tests use `run_with_fixture` to write each stage's result files and
ingest them until ranking finishes.
"""

import json
import re
from pathlib import Path

from pipeline.counterparties import is_common_heading

class FixtureError(RuntimeError):
    pass


def fixture_response(prompt):
    """Return the JSON object the offline fixture would write for this prompt."""
    task = _field(prompt, "TASK")
    if task == "significance":
        return _significance(prompt)
    if task == "centurion":
        return _centurion(prompt)
    if task == "praetorian":
        return _praetorian(prompt)
    raise FixtureError(f"fixture has no response for task {task!r}")


def run_with_fixture(db_path, *, responder=None, queue_dir=None, with_praetorians=False, **kwargs):
    """Drive a sweep offline. Not used by the live CLI."""
    from pipeline.agent import read_tasks, result_path
    from pipeline.flow import ingest_run, run_pipeline

    if queue_dir is None:
        queue_dir = str(Path(db_path).parent / "agent_queue")
    responder = responder or (lambda task: fixture_response(task["prompt"]))
    parties_client = kwargs.pop("parties_client", None)
    result = run_pipeline(db_path, queue_dir=queue_dir, with_praetorians=with_praetorians, **kwargs)
    prompts = []
    seen = set()
    while result.get("status") == "awaiting_agents":
        tasks = read_tasks(queue_dir, result["run_id"], result["stage"])
        if not tasks:
            break
        for task, path in tasks:
            key = str(path)
            if key not in seen:
                prompts.append(task["prompt"])
                seen.add(key)
            answer = result_path(path)
            if not answer.is_file():
                payload = responder(task)
                answer.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        result = ingest_run(
            db_path,
            result["run_id"],
            queue_dir=queue_dir,
            parties_client=parties_client,
        )
    result["prompts"] = prompts
    return result


def _field(prompt, key):
    prefix = key + ": "
    for line in (prompt or "").splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :]
    return ""


def _block(prompt, name):
    start = name + "_BEGIN"
    end = name + "_END"
    collecting = False
    lines = []
    for line in (prompt or "").splitlines():
        marker = line.strip()
        if marker == start:
            collecting = True
            continue
        if marker == end and collecting:
            break
        if collecting:
            lines.append(line)
    return "\n".join(lines)


def _stub_text(prompt):
    task = _field(prompt, "TASK")
    if task == "significance":
        return json.dumps(_significance(prompt), sort_keys=True)
    if task == "centurion":
        return json.dumps(_centurion(prompt), sort_keys=True)
    if task == "praetorian":
        return json.dumps(_praetorian(prompt), sort_keys=True)
    raise FixtureError(f"stub has no response for task {task!r}")


_LARGE = {
    "capex": 1_000_000_000,
    "debt_raise": 200_000_000,
    "orders_backlog": 100_000_000,
    "insider_buying": 100_000,
    "accumulation": 10,
}
# A quarterly print that merely exists again is not the novelty test.
# Growth at or above this percent is a sharp acceleration.
STRONG_GROWTH_PCT = 50


def _significance(prompt):
    lead_type = _field(prompt, "LEAD_TYPE") or "capex"
    text = _field(prompt, "VALUE_TEXT").casefold()
    try:
        value = float(_field(prompt, "VALUE") or "0")
    except ValueError:
        value = 0
    if "small-print" in text:
        magnitude = "small"
        magnitude_rationale = "The stored text marks this print as small."
    elif value >= _LARGE.get(lead_type, 1):
        magnitude = "large"
        magnitude_rationale = f"The stored figure {value:g} clears the large bar for {lead_type}."
    else:
        magnitude = "moderate"
        magnitude_rationale = f"The stored figure {value:g} clears the threshold and sits under the large bar."
    quarter = _field(prompt, "QUARTER_FIGURE") == "yes" and lead_type in {"capex", "orders_backlog"}
    growth = _optional_float(_field(prompt, "GROWTH_PCT"))
    if quarter:
        novelty, novelty_rationale = _growth_novelty(lead_type, growth)
    elif (
        "low-signal" in text
        or "routine" in text
        or _field(prompt, "EARLIER_FIGURE") == "yes"
        or _field(prompt, "EARLIER_SIMILAR") == "yes"
    ):
        novelty = "routine"
        if _field(prompt, "EARLIER_FIGURE") == "yes":
            novelty_rationale = "An earlier figure of this type is already stored for the ticker."
        elif _field(prompt, "EARLIER_SIMILAR") == "yes":
            novelty_rationale = "An earlier period stores a similar value, so this print is routine."
        else:
            novelty_rationale = "The stored text marks this as routine rather than a first occurrence."
    else:
        novelty = "first"
        novelty_rationale = "No earlier figure of this type is stored for the ticker."
    if "already noticed" in text:
        attention = "noticed"
        attention_rationale = "The stored text says this item was already noticed."
    else:
        attention = "unnoticed"
        attention_rationale = "No media note is stored on the figure, so attention stays unnoticed."
    age = _age_days(prompt)
    if "after earnings" in text:
        timing = "late"
        timing_rationale = "The stored text places this after earnings."
    elif age is None or age > 120:
        timing = "late"
        if age is None:
            timing_rationale = "No event date is stored, so timing stays in the late band."
        else:
            timing_rationale = f"The event is {age:g} days before the sweep, past the 120 day late band."
    elif age <= 45:
        timing = "early"
        timing_rationale = f"The event is {age:g} days before the sweep, inside the 45 day early band."
    else:
        timing = "on_time"
        timing_rationale = f"The event is {age:g} days before the sweep, inside the 46 to 120 day band."
    return {
        "magnitude": magnitude,
        "magnitude_rationale": magnitude_rationale,
        "novelty": novelty,
        "novelty_rationale": novelty_rationale,
        "attention": attention,
        "attention_rationale": attention_rationale,
        "timing": timing,
        "timing_rationale": timing_rationale,
    }


_TIMEFRAME = {
    "capex": 365,
    "orders_backlog": 180,
    "debt_raise": 270,
    "insider_buying": 90,
    "accumulation": 120,
    "macro": 120,
}


def _growth_novelty(lead_type, growth):
    """Quarterly capex and backlog novelty comes from the growth rate."""
    kind = "Capex" if lead_type == "capex" else "Backlog"
    if growth is not None and growth >= STRONG_GROWTH_PCT:
        shown = int(growth) if float(growth).is_integer() else growth
        return (
            "first",
            f"{kind} is up {shown:g} percent, a sharp acceleration rather than a routine quarterly print.",
        )
    if growth is not None:
        shown = int(growth) if float(growth).is_integer() else growth
        return (
            "routine",
            f"{kind} growth of {shown:g} percent is not a sharp acceleration.",
        )
    return (
        "first",
        "No growth rate is stored, so this quarterly print is not marked routine.",
    )


def _expected_return(lead_type, value):
    """Percent return from the figure's magnitude, not a fixed number per lead type."""
    bar = float(_LARGE.get(lead_type or "capex", 1))
    amount = value or 0
    if amount >= bar:
        return 24
    if amount >= bar / 10:
        return 12
    return 6


def _constituent_value(item, fallback):
    raw = item.get("value")
    try:
        if raw is None or raw == "":
            return fallback
        return float(raw)
    except (TypeError, ValueError):
        return fallback


def _centurion(prompt):
    lead_type = _field(prompt, "LEAD_TYPE") or "capex"
    ticker = _field(prompt, "TICKER")
    constituents = _constituents(prompt)
    suppliers, customers, buyers = _parties(prompt, constituents)
    grades = _grades(prompt)
    value = _optional_float(_field(prompt, "VALUE")) or 0
    if constituents:
        roi = max(_expected_return(item.get("lead_type") or lead_type, _constituent_value(item, value)) for item in constituents)
        roi += 3
        timeframe = min(_TIMEFRAME.get(item.get("lead_type"), 365) for item in constituents)
        price_run = _price_run(constituents)
    else:
        roi = _expected_return(lead_type, value)
        timeframe = _TIMEFRAME.get(lead_type, 180)
        price_run = None
    risks = _risks(lead_type, price_run)
    assumptions = [
        "The figures used here are the ones stored from the filing or the announcement.",
    ]
    if not _field(prompt, "MARKET_CAP"):
        assumptions.insert(0, "market cap unverified")
    macro = _field(prompt, "MACRO")
    if macro and macro != "none":
        assumptions.append(f"Macro context used: {macro}")
    where = ticker
    if constituents and suppliers:
        named = suppliers[0]
        where = named.get("ticker") or named.get("name") or ticker
    if lead_type == "macro":
        thesis = (
            f"Thesis for {ticker} macro. Cause: {_field(prompt, 'VALUE_TEXT')}. "
            "Likely effects follow from that cause. "
            "Winning tickers and losing tickers are named from that cause."
        )
    else:
        thesis = (
            f"Thesis for {ticker} {lead_type}. "
            f"A return depends on the official-source figure stored for this lead."
        )
    growth_text = _field(prompt, "GROWTH_TEXT")
    if growth_text:
        thesis = f"{thesis} {growth_text}"
    thesis += _named_line("Suppliers named", suppliers, lead_type == "capex")
    thesis += _named_line("Customers named", customers, lead_type == "orders_backlog")
    if buyers:
        thesis += " Buyers named: " + ", ".join(item["name"] for item in buyers) + "."
    return {
        "thesis": thesis,
        "roi_pct": roi,
        "roi": f"{roi}%",
        "timeframe_days": timeframe,
        "risks": risks,
        "recommendation": where,
        "assumptions": assumptions,
        "grades": grades,
        "suppliers": suppliers,
        "customers": customers,
        "buyers": buyers,
        "price_run": price_run,
    }


def _grades(prompt):
    market = _optional_float(_field(prompt, "MARKET_CAP"))
    value = _optional_float(_field(prompt, "VALUE"))
    grades = []
    for line in _block(prompt, "CRITERIA").splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        parts = [part.strip() for part in line[2:].split(" | ")]
        name = parts[0] if parts else ""
        if not name:
            continue
        threshold = _optional_float(parts[1]) if len(parts) > 1 else None
        if "market_cap" in name:
            if market is None:
                passed = False
                evidence = "market cap unverified"
            elif threshold is not None and market >= threshold:
                passed = True
                evidence = "shares outstanding times an official price"
            else:
                passed = False
                evidence = "market cap is below the threshold"
        elif name == "raise_vs_company":
            if market is None:
                passed = False
                evidence = "market cap unverified"
            elif value is not None and threshold is not None and (100.0 * value / market) >= threshold:
                passed = True
                evidence = "the raise clears the percent of the official market cap"
            else:
                passed = False
                evidence = "the raise is below the percent of market cap"
        elif "growth" in name:
            growth_text = _field(prompt, "GROWTH_TEXT")
            passed = "PRIOR_COMPARISON: yes" in prompt
            if passed and growth_text:
                evidence = growth_text
            elif passed:
                evidence = "prior comparison in the figures table"
            else:
                evidence = "no prior comparison in the figures table"
        else:
            passed = True
            evidence = "the stored figure is an official-source row"
        grades.append({"name": name, "passed": passed, "evidence": evidence})
    return grades


def _parties(prompt, constituents):
    if constituents:
        suppliers, customers, buyers = [], [], []
        for item in constituents:
            suppliers.extend(item.get("suppliers") or [])
            customers.extend(item.get("customers") or [])
            buyers.extend(item.get("buyers") or [])
        extra_suppliers, extra_customers, extra_buyers = _counterparty_parties(prompt)
        return (
            _unique_parties(suppliers + extra_suppliers),
            _unique_parties(customers + extra_customers),
            _unique_parties(buyers + extra_buyers),
        )
    value_text = _field(prompt, "VALUE_TEXT")
    figure_type = _field(prompt, "FIGURE_TYPE")
    instrument = _field(prompt, "INSTRUMENT")
    suppliers = [{"name": name, "ticker": ""} for name in _labeled(value_text, "supplier")]
    customers = [{"name": name, "ticker": ""} for name in _labeled(value_text, "customer")]
    buyers = [{"name": name, "ticker": ""} for name in _labeled(value_text, "buyer")]
    if figure_type == "supplier_order" and _is_party(instrument):
        buyers.append({"name": instrument.strip(), "ticker": ""})
    if figure_type in {"customer_backlog", "backlog"} and _is_party(instrument):
        customers.append({"name": instrument.strip(), "ticker": ""})
    extra_suppliers, extra_customers, extra_buyers = _counterparty_parties(prompt)
    return (
        _unique_parties(suppliers + extra_suppliers),
        _unique_parties(customers + extra_customers),
        _unique_parties(buyers + extra_buyers),
    )


def _counterparty_parties(prompt):
    suppliers, customers, buyers = [], [], []
    for line in _block(prompt, "COUNTERPARTIES").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        name = (item.get("name") or "").strip()
        if not name:
            continue
        party = {"name": name, "ticker": (item.get("ticker") or "").strip()}
        role = (item.get("role") or "").casefold()
        if role == "supplier":
            suppliers.append(party)
        elif role == "customer":
            customers.append(party)
        elif role == "buyer":
            buyers.append(party)
    return suppliers, customers, buyers


def _constituents(prompt):
    items = []
    for line in _block(prompt, "CONSTITUENTS").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            items.append(item)
    return items


def _price_run(constituents):
    blob = " ".join(str(item.get("value_text") or "") for item in constituents).casefold()
    if "priced-in" in blob:
        return "priced-in"
    kinds = {item.get("lead_type") for item in constituents if item.get("lead_type")}
    if len(kinds) > 1:
        return "acceleration"
    return "new"


def _risks(lead_type, price_run):
    risks = [
        f"The {lead_type} figure can be revised in a later filing.",
        "The path from this figure to a return is an assumption, not a booked gain.",
    ]
    if price_run == "priced-in":
        risks.append("Price run: priced-in. This is a concern because the move may already be in the price.")
    elif price_run:
        risks.append(f"Price run: {price_run}. A priced-in move would be a concern.")
    return risks


_STOP_PARTIES = {
    "quarter",
    "ytd",
    "annual",
    "ttm",
    "instant",
    "ceo",
    "bond",
    "facility",
    "loan",
    "filing",
}


_METRIC_LABELS = {"orders", "order", "backlog", "revenue"}


def _is_party(text):
    cleaned = (text or "").strip()
    folded = cleaned.casefold()
    if len(cleaned) < 2 or folded in _STOP_PARTIES or folded in _METRIC_LABELS:
        return False
    if "|" in cleaned:
        return False
    if any(char.isalpha() for char in cleaned):
        if any(char.islower() for char in cleaned) and any(char.isupper() for char in cleaned[1:]):
            # CamelCase disclosure tags are not party names.
            if " " not in cleaned:
                return False
        return True
    return False


def _labeled(text, role):
    found = []
    for match in re.finditer(rf"(?i)\b{role}s?\s*:\s*([^;\n]+)", text or ""):
        name = match.group(1).strip(" .")
        if _is_party(name):
            found.append(name)
    return found


def _named_line(label, items, required):
    if items:
        return " " + label + ": " + ", ".join(item["name"] for item in items) + "."
    if required:
        return " " + label + ": none found."
    return ""


def _unique_parties(items):
    seen = set()
    kept = []
    for item in items:
        name = (item.get("name") or "").strip()
        if not name or is_common_heading(name):
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        kept.append({"name": name, "ticker": (item.get("ticker") or "").strip()})
    return kept


def _age_days(prompt):
    return _optional_float(_field(prompt, "PERIOD_AGE_DAYS"))


def _optional_float(text):
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _grade_issues(thesis):
    found = []
    for line in (thesis or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("- ") or ":" not in stripped:
            continue
        body = stripped.split(":", 1)[1].strip()
        mark, _, evidence = body.partition(".")
        mark = mark.strip().lower()
        evidence = evidence.strip().lower()
        if mark == "fail" or evidence in {"", "missing", "missing evidence", "not graded"}:
            found.append(stripped)
    return found


def _official_citation(thesis):
    text = (thesis or "").casefold()
    marks = (
        "sec.gov",
        "asx.com.au",
        "asx.api.markitdigital.com",
        "company release",
        "asx announcement",
        "asx_announcement",
    )
    return any(mark in text for mark in marks)


def _praetorian(prompt):
    price_run = _field(prompt, "PRICE_RUN")
    thesis = _block(prompt, "THESIS")
    grade_issues = _grade_issues(thesis)
    if price_run == "priced-in":
        assumptions = (
            "The price-run classification is priced-in, so the claim that this is still early does not hold."
        )
        contradicting = "A priced-in classification is counter-evidence to treating the finding as an early lead."
        stance = "agree"
        counterevidence = (
            "Agree: the stored text marks the run as priced-in, and that concern stands."
        )
        issues = 1
    elif price_run in {"new", "acceleration"}:
        assumptions = ""
        contradicting = ""
        stance = "agree"
        counterevidence = f"Agree: the {price_run} classification matches the stored official-source figures."
        issues = 0
    else:
        assumptions = ""
        contradicting = ""
        stance = ""
        counterevidence = ""
        issues = 0
    if _official_citation(thesis):
        bad_sources = ""
    else:
        bad_sources = "The thesis does not cite an SEC filing, an ASX announcement, or a company release."
        issues += 1
    if grade_issues:
        issues += len(grade_issues)
        listed = " ".join(grade_issues)
        assumptions = (assumptions + " " + listed).strip()
    elif "market cap unverified" in (thesis or "").casefold():
        issues += 1
        assumptions = (assumptions + " market cap unverified").strip()
    return {
        "assumptions_that_fail": assumptions,
        "bad_sources": bad_sources,
        "contradicting_research": contradicting,
        "other_side": (
            "The return fails if the named spending or order is cancelled before it reaches a filing the thesis can cite."
        ),
        "surviving_issues": issues,
        "price_run_stance": stance,
        "price_run_counterevidence": counterevidence,
    }
