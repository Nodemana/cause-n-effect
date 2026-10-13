"""Prompt text for the agent tasks. Rules files are included in full."""

import json
from pathlib import Path

from pipeline.load import is_quarter_figure

REPO = Path(__file__).resolve().parents[1]
RULES = REPO / "Centurions" / "rules"
MACRO_CACHE = REPO / "cache" / "macro.json"

_LEAD_ORDER = ("capex", "debt_raise", "orders_backlog", "accumulation", "insider_buying", "macro")


def rules_text(lead_type):
    path = RULES / f"{lead_type}.md"
    if not path.is_file():
        raise FileNotFoundError(f"missing Centurion rules file {path}")
    return path.read_text(encoding="utf-8")


def significance_prompt(candidate):
    figure = candidate.figure
    quarter = candidate.lead_type in {"capex", "orders_backlog"} and is_quarter_figure(
        figure, candidate.lead_type
    )
    if quarter:
        novelty_rule = (
            "This is a quarter-end capex or backlog figure. novelty follows GROWTH_PCT: "
            "growth of at least 50 percent is first, and a smaller stored growth rate is routine. "
            "An earlier quarter of the same figure is not by itself routine. "
        )
    else:
        novelty_rule = "novelty is first only when EARLIER_FIGURE is no. "
    timing_rule = ""
    if candidate.lead_type == "orders_backlog" or quarter:
        timing_rule = "timing on_time is recorded and is not a failure for this figure. "
    macro_rule = ""
    if candidate.lead_type == "macro":
        macro_rule = (
            "This is a macro signal. Judge the series move or spread in VALUE_TEXT. "
            "The series ids, values, dates, and source URLs in that text are the evidence. "
        )
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
            f"EARLIER_SIMILAR: {'yes' if getattr(figure, 'earlier_similar', False) else 'no'}",
            f"EARLIER_FIGURE: {'yes' if getattr(figure, 'earlier_type', False) else 'no'}",
            f"GROWTH_PCT: {_number(getattr(figure, 'growth_pct', None))}",
            f"QUARTER_FIGURE: {'yes' if quarter else 'no'}",
            f"PERIOD_AGE_DAYS: {_age(figure)}",
            "Reply with one JSON object and no other text.",
            "Keys: magnitude, magnitude_rationale, novelty, novelty_rationale, "
            "attention, attention_rationale, timing, timing_rationale.",
            "magnitude is large, moderate, or small. novelty is first or routine. "
            + novelty_rule
            + timing_rule
            + macro_rule
            + "attention is unnoticed or noticed. "
            "timing is early (event age at most 45 days), on_time (46 to 120 days), or late (older than 120).",
            "Each rationale is one sentence. Use only the fields above. "
            "Do not invent a figure, a source, or a media article.",
        ]
    )


def centurion_prompt(lead, criteria, pass_number, constituents=None, macro=None, related=None, show_macro=False):
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
        f"GROWTH_PCT: {_number(getattr(figure, 'growth_pct', None))}",
        f"GROWTH_TEXT: {_one_line(getattr(figure, 'growth_text', None))}",
        f"MARKET_CAP: {_number(getattr(figure, 'market_cap', None))}",
        "COUNTERPARTIES_BEGIN",
    ]
    for party in getattr(lead, "counterparties", None) or []:
        lines.append(
            json.dumps(
                {
                    "role": party.get("role") or "",
                    "name": party.get("name") or "",
                    "ticker": party.get("ticker") or "",
                    "source_url": party.get("source_url") or "",
                },
                sort_keys=True,
            )
        )
    lines.append("COUNTERPARTIES_END")
    lines.append("GATE_RESULTS_BEGIN")
    gate_results = list(getattr(lead, "gate_results", None) or [])
    if not gate_results:
        lines.append("none")
    for item in gate_results:
        version = item.get("rubric_version")
        version_text = "" if version is None else str(version)
        lines.append(
            " | ".join(
                (
                    str(item.get("name") or ""),
                    "pass" if item.get("passed") else "fail",
                    _one_line(item.get("measured_value")),
                    version_text,
                    _one_line(item.get("evidence")),
                )
            )
        )
    lines.append("GATE_RESULTS_END")
    if lead.lead_type == "macro":
        lines.append("Macro leads are not rubric-gated. Do not grade a rubric.")
    else:
        lines.append(
            "The scout gate already applied the fixed rubric version stored in rubrics. "
            "Every GATE_RESULTS line passed. Do not grade those criteria again."
        )
    _ = criteria
    lines.append("RULES_BEGIN")
    seen = []
    for lead_type in lead.grade_types:
        if lead_type not in seen:
            seen.append(lead_type)
            lines.append(rules_text(lead_type))
    lines.append("RULES_END")
    if constituents:
        lines.append("CONSTITUENTS_BEGIN")
        for item in constituents:
            lines.append(json.dumps(item, sort_keys=True))
        lines.append("CONSTITUENTS_END")
        lines.append(
            "This lead joins the constituents into one finding. "
            "Each constituent already passed its own rubric. "
            "Still write one thesis, one ROI, one set of risks, one timeframe, "
            "and one assumptions list."
        )
        lines.append(
            "Classify any prior price run as new, acceleration, or priced-in, "
            "with reasoning and evidence. Note that call as a concern where relevant. "
            "There is no automatic rejection. Do not invent a stock price."
        )
    lines.extend(_price_path_lines(lead))
    lines.append(
        "The priced-in check uses that post-signal path, not the 52-week high. "
        "A price below the 52-week high does not mean the move is still ahead. "
        "When MOVE_PASSED is yes, the move has already happened or faded: set price_run to priced-in. "
        "When the path fields are n/a, do not invent a close and do not treat the 52-week range as the check."
    )
    if show_macro:
        if macro:
            lines.append(f"MACRO: {_one_line(macro)}")
            lines.append(
                "Macro context is a concern on a capex chain and on a later merged lead. "
                "Use the macro line as supplied. Do not re-pull series and do not treat it as a company figure."
            )
        else:
            lines.append("MACRO: none")
            lines.append("No macro cache is available. Do not invent macro conditions.")
    if related is not None:
        lines.append("RELATED_FINDINGS_BEGIN")
        if related:
            for item in related:
                lines.append(json.dumps(item, sort_keys=True))
        else:
            lines.append("none")
        lines.append("RELATED_FINDINGS_END")
        lines.append(
            "RELATED_FINDINGS are capex-chain and smart-money leads from this sweep "
            "whose text matches this series. Use them when naming likely effects. "
            "A finding that is not listed here is not relevant to this series."
        )
    lines.append(
        "Reply with one JSON object and no other text. "
        "Keys: thesis, roi, roi_pct, timeframe_days, risks, recommendation, assumptions, "
        "suppliers, customers, buyers, price_run. "
        "conviction is optional: one short phrase for how firmly the thesis holds, or omit it. "
        "risks and assumptions are lists of sentences. "
        "suppliers, customers, and buyers are lists of {name, ticker} "
        "taken from COUNTERPARTIES of that role, from CONSTITUENTS, and from the figure text. "
        "A customer on a capex lead stays a customer. A deal_party is not a "
        "supplier or a customer. "
        "When COUNTERPARTIES is empty, the thesis says none found for the "
        "suppliers line on a capex lead and for the customers line on a backlog lead. "
        "price_run is null when this task has no constituents and MOVE_PASSED is not yes. "
        "When MOVE_PASSED is yes, price_run is priced-in. "
        "When constituents are present and MOVE_PASSED is not yes, price_run is new, acceleration, or priced-in. "
        "Figures only from the source URL, the constituents, the related findings, and the fields above."
    )
    return "\n".join(lines)


def praetorian_prompt(thesis_markdown, price_run, path=None):
    lines = [
        "TASK: praetorian",
        f"PRICE_RUN: {price_run or 'none'}",
        "THESIS_BEGIN",
        thesis_markdown,
        "THESIS_END",
        "STALENESS AND PRICE REACTION",
    ]
    lines.extend(_price_path_lines(path))
    lines.extend(
        [
            "This section is mandatory.",
            "Flag a flaw when the signal is older than 42 days, about six weeks, "
            "or the post-signal move has already happened or faded.",
            "Use severity high in that case. Say whether the move has likely passed, and quote these numbers.",
            "When a path field is n/a, say so. Do not invent a close. Do not use the 52-week high instead.",
            "Write every flaw in this thesis. Do not merge, rank, reject, or drop the lead.",
            "The lead continues to ranking whether or not this list is empty.",
            "Reply with one JSON object and no other text. Key: flaws.",
            "flaws is a list of {flaw, severity, evidence}.",
            "severity is low, moderate, or high.",
            "flaw and evidence are non-empty strings.",
            "An empty list means the thesis has no recorded flaw.",
        ]
    )
    return "\n".join(lines)


def _price_path_lines(source):
    """Lines the Centurion and the Praetorian both read. Missing numbers stay n/a."""
    if source is None:
        source = {}
    if not isinstance(source, dict):
        source = {
            "signal_date": getattr(source, "signal_date", None),
            "days_since_signal": getattr(source, "days_since_signal", None),
            "close_before_signal": getattr(source, "close_before_signal", None),
            "max_runup_pct": getattr(source, "max_runup_pct", None),
            "current_vs_pre_pct": getattr(source, "current_vs_pre_pct", None),
            "popped_and_faded": getattr(source, "popped_and_faded", None),
            "move_passed": getattr(source, "move_passed", None),
        }

    def raw(name):
        value = source.get(name)
        if value is None or value == "":
            return "n/a"
        return str(value)

    def pct(name):
        value = source.get(name)
        if value is None or value == "":
            return "n/a"
        return f"{float(value):+.1f}"

    def flag(name):
        value = source.get(name)
        if value is True:
            return "yes"
        if value is False:
            return "no"
        return "n/a"

    return [
        f"SIGNAL_DATE: {raw('signal_date')}",
        f"DAYS_SINCE_SIGNAL: {raw('days_since_signal')}",
        f"CLOSE_BEFORE_SIGNAL: {raw('close_before_signal')}",
        f"MAX_RUNUP_PCT: {pct('max_runup_pct')}",
        f"CURRENT_VS_PRE_PCT: {pct('current_vs_pre_pct')}",
        f"POPPED_AND_FADED: {flag('popped_and_faded')}",
        f"MOVE_PASSED: {flag('move_passed')}",
    ]


def legate_selection_prompt(candidates):
    lines = [
        "TASK: legate_selection",
        "Select at most 5 lead ids from the candidates below.",
        "Reply with one JSON object and no other text. Keys: lead_ids, rationale.",
        "lead_ids is an array of integers copied from the LEAD_ID lines. At most 5. No duplicates.",
        "rationale is an object. Each key is a chosen lead id written as text. Each value is a non-empty string.",
        "An empty lead_ids array selects none, and rationale is then {}.",
        "The candidate report lists every thesis and its Praetorian flaws.",
        "Earliness is the signal date and the post-signal price reaction, not the 52-week high.",
        "Do not select a signal older than six weeks, or one whose post-signal move has already happened or faded, unless a newer confirming signal is also in this list.",
    ]
    for item in candidates:
        lines.extend(
            [
                f"LEAD_ID: {item['lead_id']}",
                f"TICKER: {item['ticker']}",
                f"RANK: {item['rank']}",
                f"SCOUT: {item['source_scout']}",
                f"SUMMARY: {item['summary']}",
                f"FLAWS: {item['flaws']}",
            ]
        )
    return "\n".join(lines)


def load_macro(conn, path=None):
    """Macro text for the second pass.

    Observations in `macro_series` / `macro_observations` win when those
    tables exist and have rows. Otherwise the JSON cache is used. A missing
    or empty cache returns None, and the caller skips macro context.
    """
    cached = _macro_from_tables(conn)
    if cached:
        return cached
    return _macro_from_file(path)


def ordered_types(types):
    wanted = set(types)
    return [lead_type for lead_type in _LEAD_ORDER if lead_type in wanted]


def _macro_from_tables(conn):
    if conn is None or not _macro_tables(conn):
        return None
    series_cols = _table_columns(conn, "macro_series")
    obs_cols = _table_columns(conn, "macro_observations")
    if "series_id" not in series_cols or "series_id" not in obs_cols or "obs_date" not in obs_cols:
        return None
    name_sql = "s.name" if "name" in series_cols else "s.series_id"
    source_sql = "s.source" if "source" in series_cols else "''"
    unit_sql = "s.unit" if "unit" in series_cols else "''"
    value_sql = "o.value" if "value" in obs_cols else "NULL"
    text_sql = "o.value_text" if "value_text" in obs_cols else "NULL"
    rows = conn.execute(
        f"""
        SELECT s.series_id AS series_id,
               {name_sql} AS name,
               {source_sql} AS source,
               {unit_sql} AS unit,
               o.obs_date AS obs_date,
               {value_sql} AS value,
               {text_sql} AS value_text
        FROM macro_observations AS o
        JOIN macro_series AS s ON s.series_id = o.series_id
        WHERE o.obs_date = (
            SELECT MAX(later.obs_date)
            FROM macro_observations AS later
            WHERE later.series_id = o.series_id
        )
        ORDER BY s.series_id
        """
    ).fetchall()
    lines = []
    present = set()
    for row in rows:
        reading = _observation_text(row["value_text"], row["value"])
        if not reading:
            continue
        present.add(row["series_id"])
        unit = f" {row['unit']}" if row["unit"] else ""
        source = f" ({row['source']})" if row["source"] else ""
        name = row["name"] or row["series_id"]
        lines.append(f"{row['series_id']} {name}: {reading}{unit} on {row['obs_date']}{source}")
    if not lines:
        return None
    text = "Macro scout cache: " + "; ".join(lines)
    missing = _unavailable_note(present)
    if missing:
        text = f"{text}. {missing}"
    return text


def _unavailable_note(present_ids):
    """Catalog series with no stored observation, grouped by source and reason."""
    from Scouts.macro.catalog import SERIES
    from Scouts.macro.fetcher import gap_reason, read_keys

    keys = read_keys()
    groups = {}
    order = []
    for spec in SERIES:
        if spec["series_id"] in present_ids:
            continue
        label = f"{spec['source']} ({gap_reason(spec, keys)})"
        if label not in groups:
            groups[label] = []
            order.append(label)
        groups[label].append(spec["series_id"])
    if not order:
        return ""
    parts = [f"{label}: {', '.join(groups[label])}" for label in order]
    return "unavailable: " + "; ".join(parts)


def _macro_tables(conn):
    rows = conn.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type = 'table' AND name IN ('macro_series', 'macro_observations')
        """
    ).fetchall()
    found = {row["name"] for row in rows}
    return "macro_series" in found and "macro_observations" in found


def _table_columns(conn, table):
    if table not in {"macro_series", "macro_observations"}:
        return set()
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _observation_text(value_text, value):
    text = " ".join(str(value_text or "").split())
    if text:
        return text
    if value is None or value == "":
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _macro_from_file(path):
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


def _one_line(value):
    return " ".join(str(value or "").split())


def _age(figure):
    age = getattr(figure, "period_age_days", None)
    if age is None:
        return ""
    return str(int(age))


def _number(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)
