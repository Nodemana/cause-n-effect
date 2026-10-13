"""JSON checkpoint for a sweep that is waiting on agents.

Figure rows and planned leads round-trip so ingest can resume without
recomputing growth or market cap.
"""

import json
from pathlib import Path

from pipeline.load import Figure
from pipeline.prefilter import Candidate, PlannedReject, Rating

_FIGURE_ROW = (
    "figure_id",
    "run_id",
    "scout",
    "ticker",
    "company",
    "figure_type",
    "instrument",
    "period",
    "value",
    "value_text",
    "currency",
    "seen_at",
    "announced_on",
    "source_url",
    "source_type",
    "verified",
)
_FIGURE_EXTRA = (
    "market_cap",
    "market_cap_unverified",
    "growth_pct",
    "growth_text",
    "earlier_similar",
    "earlier_type",
    "prior_comparison",
    "aggregated_ids",
    "growth_unverified",
    "period_age_days",
)
_RATING = (
    "magnitude",
    "magnitude_rationale",
    "novelty",
    "novelty_rationale",
    "attention",
    "attention_rationale",
    "timing",
    "timing_rationale",
)
_LEAD = (
    "synthetic_id",
    "lead_type",
    "ticker",
    "exchange",
    "company",
    "source_scout",
    "source_url",
    "summary",
    "figure_ids",
    "parent_position_id",
    "grade_types",
    "link_kind",
    "significance_issue",
    "market_cap_unverified",
    "growth_unverified",
    "rank",
    "presented",
    "rank_rationale",
    "real_id",
    "real_thesis_id",
    "real_merge_id",
    "pick_order",
    "selection_rationale",
    "signal_date",
    "days_since_signal",
    "close_before_signal",
    "max_runup_pct",
    "current_vs_pre_pct",
    "popped_and_faded",
    "move_passed",
)
_THESIS = (
    "markdown",
    "risks_text",
    "assumptions_text",
    "recommendation",
    "conviction",
    "roi",
    "roi_pct",
    "timeframe_days",
    "price_run",
    "macro_context",
    "pass_number",
    "grades_by_name",
    "criteria",
    "source_url",
    "suppliers",
    "customers",
    "buyers",
    "review",
    "surviving_issues",
    "risk_count",
)


class SweepState:
    def __init__(self):
        self.run_id = None
        self.started = None
        self.stage = "significance"
        self.macro = None
        self.names = {}
        self.candidates = []
        self.leads = []
        self.merged = []
        self.final_ids = []
        self.judgement_rejects = []
        self.queue_dir = ""
        self.reports_dir = ""
        self.limit = None
        self.finished = False
        self.figures = 0
        self.deterministic_rejects = 0
        self.pending_persist = False
        self.screened = False
        self.waiting = []


def state_path(queue_dir, run_id):
    return Path(queue_dir) / run_id / "state.json"


def save_state(state):
    path = state_path(state.queue_dir, state.run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(_dump_state(state), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def load_state(queue_dir, run_id):
    path = state_path(queue_dir, run_id)
    if not path.is_file():
        raise FileNotFoundError(f"missing sweep checkpoint {path}")
    return _load_state(json.loads(path.read_text(encoding="utf-8")))


def _dump_state(state):
    return {
        "run_id": state.run_id,
        "started": state.started,
        "stage": state.stage,
        "macro": state.macro,
        "names": state.names,
        "candidates": [_dump_candidate(item) for item in state.candidates],
        "leads": [_dump_lead(item) for item in state.leads],
        "merged": [_dump_lead(item) for item in state.merged],
        "final_ids": list(state.final_ids),
        "judgement_rejects": [_dump_reject(item) for item in state.judgement_rejects],
        "queue_dir": state.queue_dir,
        "reports_dir": state.reports_dir,
        "limit": state.limit,
        "finished": bool(getattr(state, "finished", False)),
        "figures": state.figures,
        "deterministic_rejects": state.deterministic_rejects,
        "pending_persist": bool(state.pending_persist),
        "screened": bool(getattr(state, "screened", False)),
        "waiting": [_dump_candidate(item) for item in (getattr(state, "waiting", None) or [])],
    }


def _load_state(data):
    state = SweepState()
    state.run_id = data["run_id"]
    state.started = data["started"]
    state.stage = data["stage"]
    state.macro = data.get("macro")
    state.names = data.get("names") or {}
    state.candidates = [_load_candidate(item) for item in data.get("candidates") or []]
    state.leads = [_load_lead(item) for item in data.get("leads") or []]
    by_id = {lead.synthetic_id: lead for lead in state.leads}
    state.merged = [_load_lead(item, by_id) for item in data.get("merged") or []]
    state.final_ids = list(data.get("final_ids") or [])
    state.judgement_rejects = [_load_reject(item) for item in data.get("judgement_rejects") or []]
    state.queue_dir = data.get("queue_dir") or ""
    state.reports_dir = data.get("reports_dir") or ""
    state.limit = data.get("limit")
    state.finished = bool(data.get("finished"))
    state.figures = data.get("figures") or 0
    state.deterministic_rejects = data.get("deterministic_rejects") or 0
    state.pending_persist = bool(data.get("pending_persist"))
    state.screened = bool(data.get("screened"))
    state.waiting = [_load_candidate(item) for item in data.get("waiting") or []]
    return state


def _dump_figure(figure):
    row = {key: getattr(figure, key) for key in _FIGURE_ROW}
    for key in _FIGURE_EXTRA:
        if hasattr(figure, key):
            value = getattr(figure, key)
            if isinstance(value, tuple):
                value = list(value)
            row[key] = value
    return row


def _load_figure(data):
    row = {key: data.get(key) for key in _FIGURE_ROW}
    row["instrument"] = row["instrument"] or ""
    row["period"] = row["period"] or ""
    row["value_text"] = row["value_text"] or ""
    row["source_url"] = row["source_url"] or ""
    row["source_type"] = row["source_type"] or ""
    row["seen_at"] = row["seen_at"] or ""
    row["verified"] = int(row["verified"] or 0)
    figure = Figure(row)
    for key in _FIGURE_EXTRA:
        if key not in data:
            continue
        value = data[key]
        if key == "aggregated_ids" and value is not None:
            value = tuple(value)
        setattr(figure, key, value)
    return figure


def _dump_rating(rating):
    if rating is None:
        return None
    return {key: getattr(rating, key) for key in _RATING}


def _load_rating(data):
    if not data:
        return None
    return Rating(**{key: data.get(key) for key in _RATING})


def _dump_thesis(thesis):
    if thesis is None:
        return None
    return {key: getattr(thesis, key) for key in _THESIS}


def _load_thesis(data):
    if not data:
        return None
    from pipeline.flow import PlannedThesis

    thesis = PlannedThesis()
    for key in _THESIS:
        setattr(thesis, key, data.get(key))
    if thesis.grades_by_name is None:
        thesis.grades_by_name = {}
    if thesis.criteria is None:
        thesis.criteria = []
    for role in ("suppliers", "customers", "buyers"):
        if getattr(thesis, role) is None:
            setattr(thesis, role, [])
    return thesis


def _dump_lead(lead, nested=False):
    payload = {key: getattr(lead, key, None) for key in _LEAD}
    payload["figure_ids"] = list(lead.figure_ids or [])
    payload["grade_types"] = list(lead.grade_types or [])
    payload["figure"] = _dump_figure(lead.figure) if lead.figure is not None else None
    payload["rating"] = _dump_rating(lead.rating)
    payload["thesis"] = _dump_thesis(lead.thesis)
    payload["counterparties"] = list(lead.counterparties or [])
    payload["parties"] = list(getattr(lead, "parties", None) or [])
    payload["related_findings"] = list(getattr(lead, "related_findings", None) or [])
    payload["gate_results"] = list(getattr(lead, "gate_results", None) or [])
    if nested:
        payload["members"] = []
        payload["member_ids"] = []
    else:
        payload["members"] = [_dump_lead(member, nested=True) for member in (lead.members or [])]
        payload["member_ids"] = [member.synthetic_id for member in (lead.members or [])]
    return payload


def _load_lead(data, by_id=None):
    from pipeline.flow import PlannedLead

    lead = PlannedLead()
    for key in _LEAD:
        setattr(lead, key, data.get(key))
    lead.figure_ids = list(data.get("figure_ids") or [])
    lead.grade_types = list(data.get("grade_types") or [])
    lead.figure = _load_figure(data["figure"]) if data.get("figure") else None
    lead.rating = _load_rating(data.get("rating"))
    lead.thesis = _load_thesis(data.get("thesis"))
    lead.counterparties = list(data.get("counterparties") or [])
    lead.parties = list(data.get("parties") or [])
    lead.related_findings = list(data.get("related_findings") or [])
    lead.gate_results = list(data.get("gate_results") or [])
    nested = data.get("members") or []
    if nested:
        lead.members = [_load_lead(item) for item in nested]
    else:
        member_ids = list(data.get("member_ids") or [])
        if by_id is None:
            lead.members = []
            lead._member_ids = member_ids
        else:
            lead.members = [by_id[item] for item in member_ids]
    return lead


def _dump_candidate(candidate):
    return {
        "figure": _dump_figure(candidate.figure),
        "lead_type": candidate.lead_type,
        "parties": list(candidate.parties or []),
        "gate_results": list(getattr(candidate, "gate_results", None) or []),
        "screen_lead_id": getattr(candidate, "screen_lead_id", None),
    }


def _load_candidate(data):
    candidate = Candidate(_load_figure(data["figure"]), data["lead_type"], list(data.get("parties") or []))
    candidate.gate_results = list(data.get("gate_results") or [])
    candidate.screen_lead_id = data.get("screen_lead_id")
    return candidate


def _dump_reject(planned):
    return {
        "figure": _dump_figure(planned.figure),
        "lead_type": planned.lead_type,
        "stage": planned.stage,
        "reason": planned.reason,
        "rating": _dump_rating(planned.rating),
        "parties": list(planned.parties or []),
    }


def _load_reject(data):
    return PlannedReject(
        _load_figure(data["figure"]),
        data["lead_type"],
        data["stage"],
        data["reason"],
        _load_rating(data.get("rating")),
        list(data.get("parties") or []),
    )
