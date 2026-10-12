"""Prompt text for the three model tasks. Rules files are included in full."""

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RULES = REPO / "Centurions" / "rules"
MACRO_CACHE = REPO / "cache" / "macro.json"

_LEAD_ORDER = ("capex", "debt_raise", "orders_backlog", "accumulation", "insider_buying")


def rules_text(lead_type):
    path = RULES / f"{lead_type}.md"
    if not path.is_file():
        raise FileNotFoundError(f"missing Centurion rules file {path}")
    return path.read_text(encoding="utf-8")


def significance_prompt(candidate):
    figure = candidate.figure
    return "\n".join(
        [
            "TASK: significance",
            f"LEAD_TYPE: {candidate.lead_type}",
            f"TICKER: {figure.ticker}",
            f"FIGURE_TYPE: {figure.figure_type}",
            f"VALUE: {_number(figure.value)}",
            f"VALUE_TEXT: {_one_line(figure.value_text)}",
            f"INSTRUMENT: {_one_line(figure.instrument)}",
            f"PERIOD: {_one_line(figure.period)}",
            f"SOURCE_URL: {_one_line(figure.source_url)}",
            "Reply with one JSON object and no other text.",
            "Keys: magnitude, magnitude_rationale, novelty, novelty_rationale, "
            "attention, attention_rationale, timing, timing_rationale.",
            "magnitude is large, moderate, or small. novelty is first or routine. "
            "attention is unnoticed or noticed. timing is early or late.",
            "Each rationale is one sentence. Use only the fields above. "
            "Do not invent a figure, a source, or a media article.",
        ]
    )


def centurion_prompt(lead, criteria, pass_number, constituents=None, macro=None):
    figure = lead.figure
    prior = "yes" if getattr(figure, "prior_comparison", False) else "no"
    lines = [
        "TASK: centurion",
        f"PASS: {pass_number}",
        f"LEAD_TYPE: {lead.lead_type}",
        f"TICKER: {lead.ticker}",
        f"COMPANY: {_one_line(lead.company)}",
        f"FIGURE_TYPE: {figure.figure_type}",
        f"VALUE: {_number(figure.value)}",
        f"VALUE_TEXT: {_one_line(figure.value_text)}",
        f"INSTRUMENT: {_one_line(figure.instrument)}",
        f"PERIOD: {_one_line(figure.period)}",
        f"SOURCE_URL: {_one_line(figure.source_url)}",
        f"PRIOR_COMPARISON: {prior}",
        "CRITERIA_BEGIN",
    ]
    for criterion in criteria:
        lines.append(f"- {criterion['name']} | {_one_line(criterion['description'])}")
    lines.append("CRITERIA_END")
    lines.append("RULES_BEGIN")
    seen = []
    for lead_type in lead.grade_types:
        if lead_type not in seen:
            seen.append(lead_type)
            lines.append(rules_text(lead_type))
    lines.append("RULES_END")
    if pass_number == 2:
        lines.append("CONSTITUENTS_BEGIN")
        for item in constituents or []:
            lines.append(json.dumps(item, sort_keys=True))
        lines.append("CONSTITUENTS_END")
        lines.append(
            "This lead is the single new lead from a Legate merge. "
            "Still write one grade, one thesis, one ROI, one set of risks, one timeframe, "
            "and one assumptions list."
        )
        lines.append(
            "Classify the price run as new, acceleration, or priced-in. "
            "priced-in is a concern, not a source of figures."
        )
        if macro:
            lines.append(f"MACRO: {_one_line(macro)}")
            lines.append("Use the macro line only as context. Do not treat it as a figure.")
        else:
            lines.append("MACRO: none")
            lines.append("No macro cache is available. Do not invent macro conditions.")
    lines.append(
        "Reply with one JSON object and no other text. "
        "Keys: thesis, roi, roi_pct, timeframe_days, risks, recommendation, assumptions, "
        "grades, suppliers, customers, buyers, price_run. "
        "grades is a list of {name, passed, evidence}. "
        "risks and assumptions are lists of sentences. "
        "suppliers, customers, and buyers are lists of {name, ticker}. "
        "price_run is null on a first pass. "
        "Figures only from the source URL and the fields above."
    )
    return "\n".join(lines)


def praetorian_prompt(thesis_markdown, price_run):
    return "\n".join(
        [
            "TASK: praetorian",
            f"PRICE_RUN: {price_run or 'none'}",
            "THESIS_BEGIN",
            thesis_markdown,
            "THESIS_END",
            "Critique only. Do not merge, rank, or add an assumptions list of your own.",
            "Reply with one JSON object and no other text. Keys: assumptions_that_fail, "
            "bad_sources, contradicting_research, other_side, surviving_issues. "
            "surviving_issues is an integer. Empty strings are allowed for the text keys.",
        ]
    )


def load_macro(path=None):
    """Return the cache summary, or None when the file is missing or empty."""
    if path is None:
        candidate = MACRO_CACHE
        if not candidate.is_file():
            return None
        path = candidate
    file_path = Path(path)
    if not file_path.is_file():
        return None
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(payload, str):
        summary = payload.strip()
    elif isinstance(payload, dict):
        summary = str(payload.get("summary") or "").strip()
    else:
        return None
    return summary or None


def ordered_types(types):
    wanted = set(types)
    return [lead_type for lead_type in _LEAD_ORDER if lead_type in wanted]


def _one_line(value):
    return " ".join(str(value or "").split())


def _number(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)
