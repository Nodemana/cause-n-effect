"""Sweep latest scout figures into pending decisions.

`run_pipeline` does the deterministic work, writes significance task files,
records status awaiting_agents, and returns. `ingest_run` checks result
files and advances one judgement stage. Combining leads, a leftover Legate
merge, and ranking stay in process and do not write task files.

Stage order: significance, then one Centurion task per lead, a leftover Legate
merge, centurion pass 2 only for that leftover, then ranking. Praetorian
review is off unless the run passes `--with-praetorians`. Ranking
writes at most `--max-theses` theses (default 5) to
`data/reports/<run_id>.md`. Linkable
capex, orders backlog, and debt raises become one capex-chain lead before
that Centurion. Same-ticker insider and accumulation signals inside the
window become one smart-money lead. Macro stays a lead of its own. Market cap
is applied in the deterministic gates from shares outstanding and an official
share-price figure.

A capex chain is linked from the parties named on the figures, not from a
thesis. A leftover merge, when one still exists, uses the suppliers,
customers, and buyers named in the theses. A shared sector is not a link.
The chain Centurion classifies the price run as new, acceleration, or
priced-in, and includes macro text only when a macro cache is present.
"""

import json
import math
from pathlib import Path

from shared.pipeline_store import connect
from shared.shares import cache_shares_outstanding, companies_from_figures
from shared.store import utc_now

from pipeline.agent import (
    SIGNIFICANCE_SCHEMA,
    CENTURION_SCHEMA,
    PRAETORIAN_SCHEMA,
    AgentResultError,
    figure_task_id,
    lead_task_id,
    load_results,
    outstanding_tasks,
    schema_for,
    write_task,
)
from pipeline.checkpoint import SweepState, load_state, save_state
from pipeline.counterparties import enrich_leads, is_common_heading
from pipeline.load import is_quarter_figure, load_latest_figures
from pipeline.macro_signals import macro_candidates
from pipeline.marketcap import UNVERIFIED
from pipeline.names import normalise_name
from pipeline.prefilter import (
    PlannedReject,
    Rating,
    candidate_exchange,
    failure_reason,
    interesting,
    party_index,
    prefilter,
    surviving_issue,
    _in_flight,
)
from pipeline.prompts import (
    centurion_prompt,
    load_macro,
    ordered_types,
    praetorian_prompt,
    rules_text,
    significance_prompt,
)
from pipeline.report import reports_directory, write_report
from pipeline.scout_groups import CHAIN_TYPES, combine
from pipeline import store

SHORTLIST_MAX = 5
# One surviving issue costs this many score points. Magnitude and growth
# can still place a large backlog or a sharp capex print above a small stake.
_ISSUE_PENALTY = 8.0
_PRICE_RUNS = {"new", "acceleration", "priced-in"}
_LEAD_TYPES = ("capex", "debt_raise", "orders_backlog", "accumulation", "insider_buying", "macro")
_AGENT_STAGES = ("significance", "centurion_1", "centurion_2", "praetorian")
_JUDGEMENT_STAGE = "agent"
PRAETORIAN_RULES = Path(__file__).resolve().parents[1] / "Praetorians" / "README.md"


class PlannedThesis:
    def __init__(self):
        self.markdown = ""
        self.risks_text = ""
        self.assumptions_text = ""
        self.recommendation = ""
        self.roi = ""
        self.roi_pct = 0
        self.timeframe_days = None
        self.price_run = None
        self.macro_context = None
        self.pass_number = 1
        self.grades_by_name = {}
        self.criteria = []
        self.source_url = ""
        self.suppliers = []
        self.customers = []
        self.buyers = []
        self.review = None
        self.surviving_issues = 0
        self.risk_count = 0


class PlannedLead:
    def __init__(self):
        self.synthetic_id = None
        self.lead_type = None
        self.ticker = None
        self.exchange = None
        self.company = None
        self.source_scout = None
        self.source_url = None
        self.summary = None
        self.figure_ids = []
        self.figure = None
        self.rating = None
        self.parent_position_id = None
        self.grade_types = []
        self.thesis = None
        self.members = []
        self.link_kind = None
        self.real_id = None
        self.real_thesis_id = None
        self.real_merge_id = None
        self.counterparties = []
        self.related_findings = []


def run_pipeline(
    db_path,
    *,
    dry_run=False,
    limit=None,
    macro_cache=None,
    now=None,
    shares_client=None,
    queue_dir=None,
    max_theses=SHORTLIST_MAX,
    with_praetorians=False,
):
    """Do the deterministic work and queue the next agent stage.

    Significance tasks are written immediately. Counterparties are named
    later, on ingest, and only when that call is given a parties client.
    This function does not judge a lead and does not call a model.
    """
    if limit is not None and int(limit) < 1:
        raise ValueError("limit must be a positive integer")
    if int(max_theses) < 1:
        raise ValueError("max_theses must be a positive integer")
    for lead_type in _LEAD_TYPES:
        rules_text(lead_type)
    started = now or utc_now()
    queue = str(Path(queue_dir or "data/agent_queue").resolve())
    conn = connect(db_path)
    try:
        macro = load_macro(conn, macro_cache)
        figures = load_latest_figures(conn, as_of=started)
        if shares_client is not None and not dry_run:
            cache_shares_outstanding(
                conn,
                companies_from_figures(figures),
                client=shares_client,
                fetched_at=started,
            )
        names, _ciks = party_index(figures)
        if dry_run:
            candidates, _rejects, used = prefilter(conn, figures, limit=limit, now=started)
            return _awaiting_result(
                SweepState(),
                status="dry_run",
                tasks=len(candidates) + len(macro_candidates(conn, started)),
                figures=used,
                macro=macro,
                dry_run=True,
            )
        state = SweepState()
        state.started = started
        state.macro = macro
        state.names = names
        state.queue_dir = queue
        state.reports_dir = reports_directory(queue)
        state.max_theses = int(max_theses)
        state.with_praetorians = bool(with_praetorians)
        state.limit = limit
        run_id = store.start_run(conn, started)
        state.run_id = run_id
        store.begin_pipeline_run(conn, run_id, started, queue, "awaiting_agents", "significance")
        tasks = _open_significance(conn, state)
        if state.stage == "ranking":
            return _complete_result(conn, state)
        save_state(state)
        store.set_pipeline_stage(conn, run_id, "awaiting_agents", state.stage)
    finally:
        conn.close()
    return _awaiting_result(state, status="awaiting_agents", tasks=tasks, figures=state.figures, macro=macro)


def ingest_run(db_path, run_id, *, queue_dir=None, parties_client=None):
    """Validate the current stage's results, store them, and advance.

    A missing or malformed result raises AgentResultError and leaves the
    stage where it is. Legate merge and ranking run here when their inputs
    are ready. Ranking writes no task files.
    """
    conn = connect(db_path)
    try:
        row = store.pipeline_run(conn, run_id)
        if row is None:
            raise AgentResultError(f"no pipeline run {run_id}")
        stored_queue = row["queue_dir"]
        if queue_dir is not None and stored_queue and str(Path(queue_dir).resolve()) != str(Path(stored_queue).resolve()):
            raise AgentResultError(
                f"queue dir {queue_dir} does not match the run's queue dir {stored_queue}"
            )
        queue = stored_queue or str(Path(queue_dir or "data/agent_queue").resolve())
        if row["finished_at"]:
            state = load_state(queue, run_id)
            if _report_path(state) is None:
                write_report(state, _ranked_from_state(state))
            return _complete_result(conn, state)
        state = load_state(queue, run_id)
        if state.pending_persist:
            _finish(conn, state, leads=state.leads, merged=state.merged, ranked=_ranked_from_state(state))
            return _complete_result(conn, state)
        stage = state.stage
        if stage not in _AGENT_STAGES:
            raise AgentResultError(f"run {run_id} is not waiting on an agent stage ({stage})")
        else:
            results = load_results(queue, run_id, stage)
            if stage == "significance":
                _apply_significance(conn, state, results, parties_client)
            elif stage == "centurion_1":
                _apply_centurion_1(conn, state, results)
            elif stage == "centurion_2":
                _apply_centurion_2(conn, state, results)
            elif stage == "praetorian":
                _apply_praetorian(conn, state, results)
        if state.pending_persist:
            _finish(conn, state, leads=state.leads, merged=state.merged, ranked=_ranked_from_state(state))
            return _complete_result(conn, state)
        save_state(state)
        store.set_pipeline_stage(conn, run_id, "awaiting_agents", state.stage)
        from pipeline.agent import task_files

        tasks = len(task_files(queue, run_id, state.stage))
    finally:
        conn.close()
    return _awaiting_result(state, status="awaiting_agents", tasks=tasks, figures=state.figures, macro=state.macro)


def pipeline_status(db_path, run_id):
    """Outstanding task files for one run."""
    conn = connect(db_path)
    try:
        row = store.pipeline_run(conn, run_id)
        if row is None:
            raise AgentResultError(f"no pipeline run {run_id}")
        stage = row["stage"]
        queue = row["queue_dir"] or ""
        if row["finished_at"] or stage not in _AGENT_STAGES:
            pending = []
        else:
            pending = outstanding_tasks(queue, run_id, stage)
    finally:
        conn.close()
    outstanding = [
        {"lead_id": path.name[: -len(".task.json")], "stage": stage, "task": str(path)}
        for path in pending
    ]
    return {
        "run_id": run_id,
        "status": row["status"],
        "stage": stage,
        "queue_dir": queue,
        "outstanding": outstanding,
    }


def _awaiting_result(state, *, status, tasks, figures, macro, dry_run=False):
    return {
        "run_id": None if dry_run else state.run_id,
        "provider": "agent",
        "tokens_used": 0,
        "token_budget": 0,
        "figures": figures,
        "leads": 0,
        "rejects": 0 if dry_run else state.deterministic_rejects,
        "merges": 0,
        "rankings": 0,
        "decisions": 0,
        "dry_run": bool(dry_run),
        "macro": _macro_log(macro, []),
        "status": status,
        "stage": "significance" if dry_run else state.stage,
        "tasks": tasks,
        "queue_dir": None if dry_run else state.queue_dir,
        "report": None,
    }


def _complete_result(conn, state):
    run_id = state.run_id
    leads = conn.execute("SELECT COUNT(*) FROM leads WHERE run_id = ?", (run_id,)).fetchone()[0]
    rejects = conn.execute("SELECT COUNT(*) FROM lead_rejects WHERE run_id = ?", (run_id,)).fetchone()[0]
    merges = conn.execute("SELECT COUNT(*) FROM legate_merges WHERE run_id = ?", (run_id,)).fetchone()[0]
    rankings = conn.execute("SELECT COUNT(*) FROM rankings WHERE run_id = ?", (run_id,)).fetchone()[0]
    decisions = conn.execute("SELECT COUNT(*) FROM decisions WHERE run_id = ?", (run_id,)).fetchone()[0]
    second = conn.execute(
        """
        SELECT COUNT(*) FROM theses
        WHERE run_id = ? AND macro_context IS NOT NULL AND length(trim(macro_context)) > 0
        """,
        (run_id,),
    ).fetchone()[0]
    if not state.macro:
        macro = "skipped"
    else:
        macro = f"loaded (second-pass theses: {second})"
    return {
        "run_id": run_id,
        "provider": "agent",
        "tokens_used": 0,
        "token_budget": 0,
        "figures": state.figures,
        "leads": leads,
        "rejects": rejects,
        "merges": merges,
        "rankings": rankings,
        "decisions": decisions,
        "dry_run": False,
        "macro": macro,
        "status": "complete",
        "stage": "ranking",
        "tasks": 0,
        "queue_dir": state.queue_dir,
        "report": _report_path(state),
    }


def _report_path(state):
    directory = getattr(state, "reports_dir", None) or reports_directory(state.queue_dir or "data/agent_queue")
    path = Path(directory) / f"{state.run_id}.md"
    if path.is_file():
        return str(path)
    return None


def _ranked_from_state(state):
    by_id = {lead.synthetic_id: lead for lead in list(state.leads) + list(state.merged)}
    final = [by_id[item] for item in state.final_ids]
    return [lead for lead in final if getattr(lead, "rank", None)]


def _open_significance(conn, state):
    """Run the gates and queue significance, or finish when nothing remains."""
    figures = load_latest_figures(conn, as_of=state.started)
    candidates, rejects, used = prefilter(conn, figures, limit=state.limit, now=state.started)
    for candidate in macro_candidates(conn, state.started):
        if _in_flight(conn, candidate):
            continue
        candidates.append(candidate)
    state.candidates = candidates
    state.figures = used
    state.deterministic_rejects = len(rejects)
    for planned in rejects:
        store.insert_reject(conn, state.run_id, planned, utc_now())
    if not candidates:
        _finish(conn, state, leads=[], merged=[], ranked=[])
        return 0
    state.stage = "significance"
    return _write_significance_tasks(state)


def _write_significance_tasks(state):
    count = 0
    for candidate in state.candidates:
        figure = candidate.figure
        lead_id = figure_task_id(figure.figure_id)
        prompt = significance_prompt(candidate)
        _write_judgement_task(
            state,
            "significance",
            lead_id,
            prompt,
            rules_text(candidate.lead_type),
            {
                "ticker": figure.ticker,
                "lead_type": candidate.lead_type,
                "company": figure.company,
            },
            {
                "figure_id": figure.figure_id,
                "ticker": figure.ticker,
                "lead_type": candidate.lead_type,
                "figure_type": figure.figure_type,
                "value": figure.value,
                "value_text": figure.value_text,
                "instrument": figure.instrument,
                "period": figure.period,
                "source_url": figure.source_url,
                "growth_pct": getattr(figure, "growth_pct", None),
                "growth_text": getattr(figure, "growth_text", None),
            },
            None,
            SIGNIFICANCE_SCHEMA,
        )
        count += 1
    return count


def _write_centurion_tasks(conn, state, leads, pass_number, stage):
    count = 0
    for lead in leads:
        criteria = store.criteria_for(conn, lead.grade_types)
        show_macro = lead.source_scout == "capex_chain" or pass_number == 2
        constituents = [_constituent_payload(member) for member in lead.members] if lead.members else None
        related = lead.related_findings if lead.lead_type == "macro" else None
        macro_context = state.macro if show_macro else None
        prompt = centurion_prompt(
            lead,
            criteria,
            pass_number,
            constituents=constituents,
            macro=macro_context,
            related=related,
            show_macro=show_macro,
        )
        rules = "\n\n".join(rules_text(lead_type) for lead_type in ordered_types(lead.grade_types))
        _write_judgement_task(
            state,
            stage,
            lead_task_id(lead.synthetic_id),
            prompt,
            rules,
            {"ticker": lead.ticker, "lead_type": lead.lead_type, "company": lead.company},
            {
                "ticker": lead.ticker,
                "company": lead.company,
                "lead_type": lead.lead_type,
                "pass_number": pass_number,
                "figure_ids": list(lead.figure_ids),
                "counterparties": list(lead.counterparties or []),
                "criteria": criteria,
                "constituents": constituents or [],
                "related_findings": related or [],
            },
            macro_context,
            CENTURION_SCHEMA,
        )
        count += 1
    return count


def _write_praetorian_tasks(state, leads):
    rules = PRAETORIAN_RULES.read_text(encoding="utf-8")
    count = 0
    for lead in leads:
        prompt = praetorian_prompt(lead.thesis.markdown, lead.thesis.price_run)
        _write_judgement_task(
            state,
            "praetorian",
            lead_task_id(lead.synthetic_id),
            prompt,
            rules,
            {"ticker": lead.ticker, "lead_type": lead.lead_type, "company": lead.company},
            {
                "thesis": lead.thesis.markdown,
                "price_run": lead.thesis.price_run,
                "source_url": lead.source_url,
                "macro_context": lead.thesis.macro_context,
            },
            lead.thesis.macro_context,
            PRAETORIAN_SCHEMA,
        )
        count += 1
    return count


def _apply_significance(conn, state, results, parties_client):
    leads = []
    next_id = 1
    for candidate in state.candidates:
        lead_id = figure_task_id(candidate.figure.figure_id)
        data = results.get(lead_id)
        if data is None:
            raise AgentResultError(f"{lead_id}: missing result for a queued figure")
        rating = Rating.from_model(data)
        ignore_on_time = _quarter_timing(candidate)
        if not interesting(rating, ignore_on_time=ignore_on_time):
            state.judgement_rejects.append(
                PlannedReject(
                    candidate.figure,
                    candidate.lead_type,
                    _JUDGEMENT_STAGE,
                    failure_reason(rating, ignore_on_time=ignore_on_time),
                    rating,
                    candidate.parties,
                )
            )
            continue
        lead = _lead_from_candidate(candidate, rating)
        lead.significance_issue = surviving_issue(rating, ignore_on_time=ignore_on_time)
        lead.synthetic_id = next_id
        next_id += 1
        lead.parent_position_id = store.open_position_id(conn, lead.ticker)
        leads.append(lead)
    enrich_leads(leads, client=parties_client, names=state.names, as_of=state.started)
    state.leads = combine(leads)
    state.candidates = []
    if not state.leads:
        _queue_ranking(conn, state)
        return
    _write_centurion_tasks(conn, state, state.leads, 1, "centurion_1")
    state.stage = "centurion_1"


def _apply_centurion_1(conn, state, results):
    for lead in state.leads:
        lead_id = lead_task_id(lead.synthetic_id)
        data = results.get(lead_id)
        if data is None:
            raise AgentResultError(f"{lead_id}: missing centurion result")
        criteria = store.criteria_for(conn, lead.grade_types)
        macro = state.macro if lead.source_scout == "capex_chain" else None
        lead.thesis = _thesis_from_model(data, lead, criteria, 1, macro=macro)
        _resolve_parties(lead.thesis, state.names)
    state.leads = _drop_hard_failures(state.leads, state.judgement_rejects)
    merged = _merge(conn, state.leads, state.macro)
    state.merged = merged
    if merged:
        _write_centurion_tasks(conn, state, merged, 2, "centurion_2")
        state.stage = "centurion_2"
        return
    _queue_praetorian(conn, state)


def _apply_centurion_2(conn, state, results):
    for lead in state.merged:
        lead_id = lead_task_id(lead.synthetic_id)
        data = results.get(lead_id)
        if data is None:
            raise AgentResultError(f"{lead_id}: missing second-pass result")
        criteria = store.criteria_for(conn, lead.grade_types)
        lead.thesis = _thesis_from_model(data, lead, criteria, 2, macro=state.macro)
        _resolve_parties(lead.thesis, state.names)
    state.merged = _drop_hard_failures(state.merged, state.judgement_rejects)
    _queue_praetorian(conn, state)


def _apply_praetorian(conn, state, results):
    by_id = {lead.synthetic_id: lead for lead in list(state.leads) + list(state.merged)}
    final = [by_id[item] for item in state.final_ids]
    for lead in final:
        lead_id = lead_task_id(lead.synthetic_id)
        data = results.get(lead_id)
        if data is None:
            raise AgentResultError(f"{lead_id}: missing praetorian result")
        _attach_review(lead, data)
    ranked = _rank(final, state.max_theses, with_praetorians=True)
    state.final_ids = [lead.synthetic_id for lead in final]
    state.pending_persist = True
    state.stage = "praetorian"
    save_state(state)
    _ = (conn, ranked)


def _queue_praetorian(conn, state):
    final = _final_leads(state.leads, state.merged)
    state.final_ids = [lead.synthetic_id for lead in final]
    if not final or not state.with_praetorians:
        _queue_ranking(conn, state)
        return
    _write_praetorian_tasks(state, final)
    state.stage = "praetorian"


def _queue_ranking(conn, state):
    final = _final_leads(state.leads, state.merged)
    state.final_ids = [lead.synthetic_id for lead in final]
    ranked = _rank(final, state.max_theses, with_praetorians=state.with_praetorians)
    state.pending_persist = True
    state.stage = "ranking"
    save_state(state)
    _ = (conn, ranked)


def _write_judgement_task(state, stage, lead_id, prompt, rules, subject, evidence, macro_context, output_schema):
    schema_for(stage)
    write_task(
        state.queue_dir,
        state.run_id,
        stage,
        lead_id,
        {
            "run_id": state.run_id,
            "stage": stage,
            "lead_id": lead_id,
            "subject": subject,
            "prompt": prompt,
            "rules": rules,
            "evidence": evidence,
            "macro_context": macro_context,
            "output_schema": output_schema,
        },
    )


def _macro_log(macro, merged):
    if not macro:
        return "skipped"
    second_pass = sum(
        1
        for lead in merged
        if getattr(lead, "thesis", None) is not None and lead.thesis.pass_number == 2
    )
    return f"loaded (second-pass theses: {second_pass})"


def _quarter_timing(candidate):
    """Every orders_backlog figure ignores on_time. Capex does so only for a quarter print."""
    if candidate.lead_type == "orders_backlog":
        return True
    if candidate.lead_type == "capex":
        return is_quarter_figure(candidate.figure, "capex")
    return False


def _lead_from_candidate(candidate, rating):
    figure = candidate.figure
    lead = PlannedLead()
    lead.lead_type = candidate.lead_type
    lead.ticker = figure.ticker
    lead.exchange = candidate_exchange(candidate)
    lead.company = figure.company
    lead.source_scout = figure.scout
    lead.source_url = figure.source_url or None
    lead.summary = " ".join(
        part for part in (figure.figure_type, figure.value_text or _plain(figure.value), figure.period) if part
    )
    if candidate.lead_type == "macro":
        lead.figure_ids = []
        lead.exchange = "MACRO"
        lead.source_scout = "macro"
    else:
        lead.figure_ids = [figure.figure_id]
        for extra in getattr(figure, "aggregated_ids", ()) or ():
            if extra not in lead.figure_ids:
                lead.figure_ids.append(extra)
    lead.figure = figure
    lead.rating = rating
    lead.grade_types = [candidate.lead_type]
    lead.parties = list(candidate.parties)
    lead.market_cap_unverified = bool(getattr(figure, "market_cap_unverified", False))
    lead.growth_unverified = bool(getattr(figure, "growth_unverified", False))
    return lead


HARD_CRITERIA = {
    "min_market_cap",
    "capex_growth_yoy",
    "raise_vs_company",
    "backlog_qoq_growth",
    "insider_buy_dollars",
    "min_stake_pct",
    "recent_window",
}


def _drop_hard_failures(leads, rejects):
    kept = []
    for lead in leads:
        reason = _hard_grade_reason(lead)
        if not reason:
            kept.append(lead)
            continue
        parties = getattr(lead, "parties", None) or []
        rejects.append(
            PlannedReject(
                lead.figure,
                lead.lead_type,
                _JUDGEMENT_STAGE,
                reason,
                lead.rating,
                parties,
            )
        )
    return kept


def _hard_grade_reason(lead):
    thesis = lead.thesis
    if thesis is None:
        return ""
    failed = []
    seen = set()
    for name, grade in thesis.grades_by_name.items():
        if name not in HARD_CRITERIA:
            continue
        seen.add(name)
        if _unverified_grade(lead, name, grade.get("evidence")):
            continue
        if _open_growth_grade(lead, name, grade.get("evidence")):
            continue
        if grade.get("passed"):
            evidence = _text(grade.get("evidence")).lower()
            if evidence in {"", "missing", "missing evidence", "not graded"}:
                failed.append(f"failed {name}: missing evidence")
            continue
        evidence = _text(grade.get("evidence")) or "missing evidence"
        failed.append(f"failed {name}: {evidence}")
    for criterion in thesis.criteria or []:
        name = criterion.get("name")
        if name in HARD_CRITERIA and name not in seen:
            if _unverified_grade(lead, name, "") or _open_growth_grade(lead, name, ""):
                continue
            failed.append(f"failed {name}: missing evidence")
    return "; ".join(failed)


def _open_growth_grade(lead, name, evidence):
    """A large order with no stored prior is a surviving issue, not a hard reject."""
    if name != "backlog_qoq_growth" or not getattr(lead, "growth_unverified", False):
        return False
    text = _text(evidence).lower()
    if text in {"", "missing", "missing evidence", "not graded"}:
        return True
    return "no prior" in text


def _unverified_grade(lead, name, evidence):
    """An unknown market cap is a surviving issue, not a hard reject."""
    if not getattr(lead, "market_cap_unverified", False):
        return False
    if name not in {"min_market_cap", "raise_vs_company"}:
        return False
    text = _text(evidence).lower()
    if text in {"", "market cap unverified", UNVERIFIED}:
        return True
    return UNVERIFIED in text


def _merge(conn, leads, macro):
    mergeable = [lead for lead in leads if lead.thesis is not None and lead.parent_position_id is None]
    merged = []
    next_id = 1 + max((lead.synthetic_id for lead in leads), default=0)
    for kind, members in _components(mergeable):
        primary = _primary(kind, members)
        lead = PlannedLead()
        lead.synthetic_id = next_id
        next_id += 1
        lead.lead_type = primary.lead_type
        lead.ticker = primary.ticker
        lead.exchange = primary.exchange
        lead.company = primary.company
        lead.source_scout = "legate"
        lead.source_url = primary.source_url
        lead.figure = primary.figure
        lead.figure_ids = []
        for member in members:
            for figure_id in member.figure_ids:
                if figure_id not in lead.figure_ids:
                    lead.figure_ids.append(figure_id)
        lead.rating = primary.rating
        lead.grade_types = ordered_types(member.lead_type for member in members)
        lead.members = members
        lead.counterparties = _merged_counterparties(members)
        lead.link_kind = kind
        lead.summary = _rationale(kind, members)
        lead.market_cap_unverified = bool(
            getattr(primary, "market_cap_unverified", False)
            or getattr(primary.figure, "market_cap_unverified", False)
        )
        lead.growth_unverified = any(
            getattr(member, "growth_unverified", False)
            or getattr(member.figure, "growth_unverified", False)
            for member in members
        )
        issues = []
        for member in members:
            issue = getattr(member, "significance_issue", None)
            if issue and issue not in issues:
                issues.append(issue)
        lead.significance_issue = "; ".join(issues) if issues else None
        lead.thesis = None
        merged.append(lead)
    _ = (conn, macro)
    return merged


def _final_leads(leads, merged):
    consumed = {member.synthetic_id for lead in merged for member in lead.members}
    final = []
    for lead in leads:
        if lead.synthetic_id in consumed or lead.parent_position_id is not None:
            continue
        if lead.thesis is not None:
            final.append(lead)
    for lead in merged:
        if lead.thesis is not None:
            final.append(lead)
    return final


def _attach_review(lead, data):
    stance = _text(data.get("price_run_stance")).casefold()
    review = {
        "assumptions_that_fail": _text(data.get("assumptions_that_fail")),
        "bad_sources": _text(data.get("bad_sources")),
        "contradicting_research": _text(data.get("contradicting_research")),
        "other_side": _text(data.get("other_side")),
        "surviving_issues": max(0, int(data.get("surviving_issues") or 0)),
        "price_run_stance": stance if stance in {"agree", "disagree"} else "",
        "price_run_counterevidence": _text(data.get("price_run_counterevidence")),
    }
    _note_unverified_cap(lead, review)
    _note_significance(lead, review)
    lead.thesis.review = review
    lead.thesis.surviving_issues = review["surviving_issues"]


def _note_significance(lead, review):
    """One routine or on_time rating stays on the lead as a surviving issue."""
    issue = getattr(lead, "significance_issue", None)
    if not issue:
        return
    if issue.casefold() in review["assumptions_that_fail"].casefold():
        return
    review["assumptions_that_fail"] = (review["assumptions_that_fail"] + " " + issue).strip()
    review["surviving_issues"] += 1


def _note_unverified_cap(lead, review):
    if not getattr(lead, "market_cap_unverified", False):
        return
    if UNVERIFIED in review["assumptions_that_fail"].casefold():
        return
    review["assumptions_that_fail"] = (review["assumptions_that_fail"] + " " + UNVERIFIED).strip()
    review["surviving_issues"] += 1


def _rank(final, max_theses=SHORTLIST_MAX, *, with_praetorians=False):
    cap = SHORTLIST_MAX if max_theses is None else int(max_theses)
    reviewed = []
    for lead in final:
        if lead.thesis is None:
            continue
        if with_praetorians:
            if lead.thesis.review is None:
                continue
        else:
            lead.thesis.review = None
            lead.thesis.surviving_issues = _issues_without_praetorian(lead)
        reviewed.append(lead)
    scored = [( _rank_score(lead), lead) for lead in reviewed]
    scored.sort(key=lambda item: _rank_key(item[1], item[0]))
    for index, (score, lead) in enumerate(scored, start=1):
        lead.rank = index
        lead.presented = index <= cap
        lead.rank_rationale = (
            f"Rank {index}: score {score:.1f} from ROI {lead.thesis.roi or lead.thesis.roi_pct}, "
            f"growth and magnitude, minus {lead.thesis.surviving_issues} surviving issues; "
            f"timeframe {lead.thesis.timeframe_days} days, "
            f"{lead.thesis.risk_count} risks."
        )
    return [lead for _score, lead in scored]


def _deterministic_notes(lead):
    """Soft gates recorded on the thesis when Praetorian review is off."""
    notes = []
    if getattr(lead, "market_cap_unverified", False):
        notes.append(UNVERIFIED)
    issue = getattr(lead, "significance_issue", None)
    if issue:
        notes.append(issue)
    if getattr(lead, "growth_unverified", False):
        notes.append("no prior comparison in the figures table")
    return notes


def _issues_without_praetorian(lead):
    """Soft gates that are not a Praetorian verdict."""
    return len(_deterministic_notes(lead))


def _rank_key(lead, score):
    thesis = lead.thesis
    return (
        -score,
        thesis.surviving_issues,
        -float(thesis.roi_pct or 0),
        thesis.timeframe_days if thesis.timeframe_days is not None else 10**9,
        thesis.risk_count,
        lead.synthetic_id,
    )


def _rank_score(lead):
    """Expected return, plus growth and size, minus a penalty per surviving issue."""
    roi = float(lead.thesis.roi_pct or 0)
    growth = _best_growth(lead)
    growth_pts = min(max(growth, 0.0), 300.0) / 10.0
    return roi + growth_pts + _magnitude_points(lead) - (lead.thesis.surviving_issues or 0) * _ISSUE_PENALTY


def _best_growth(lead):
    best = 0.0
    for _lead_type, figure in _rank_figures(lead):
        raw = getattr(figure, "growth_pct", None)
        if raw is None:
            continue
        try:
            best = max(best, float(raw))
        except (TypeError, ValueError):
            continue
    return best


def _magnitude_points(lead):
    best = 0.0
    for lead_type, figure in _rank_figures(lead):
        best = max(best, _one_magnitude(lead_type, figure))
    return best


def _one_magnitude(lead_type, figure):
    try:
        value = float(figure.value)
    except (TypeError, ValueError):
        return 0.0
    if value <= 0:
        return 0.0
    if lead_type == "accumulation":
        return min(value, 100.0) / 10.0
    return math.log10(value)


def _rank_figures(lead):
    rows = []
    if getattr(lead, "figure", None) is not None:
        rows.append((lead.lead_type, lead.figure))
    for member in getattr(lead, "members", ()) or ():
        if getattr(member, "figure", None) is not None:
            rows.append((member.lead_type, member.figure))
    return rows


def _finish(conn, state, leads, merged, ranked):
    row = store.pipeline_run(conn, state.run_id)
    if row is None or row["finished_at"] is None:
        _persist(conn, state.run_id, state.judgement_rejects, leads, merged, ranked)
    write_report(state, ranked)
    state.pending_persist = False
    state.stage = "ranking"
    save_state(state)


def _persist(conn, run_id, rejects, leads, merged, ranked):
    created = utc_now()
    for planned in rejects:
        store.insert_reject(conn, run_id, planned, created)
    id_map = {}
    for lead in leads:
        lead.real_id = store.insert_lead(conn, run_id, lead, created)
        id_map[lead.synthetic_id] = lead.real_id
        if lead.thesis is not None:
            lead.real_thesis_id = store.insert_thesis(conn, run_id, lead.real_id, lead.thesis, created)
    for lead in merged:
        lead.real_id = store.insert_lead(conn, run_id, lead, created)
        id_map[lead.synthetic_id] = lead.real_id
        if lead.thesis is not None:
            lead.real_thesis_id = store.insert_thesis(conn, run_id, lead.real_id, lead.thesis, created)
        evidence = json.dumps(_evidence(lead.members, id_map), sort_keys=True)
        lead.real_merge_id = store.insert_merge(
            conn,
            run_id,
            lead.real_id,
            [id_map[member.synthetic_id] for member in lead.members],
            lead.summary,
            evidence,
            created,
        )
    for lead in ranked:
        review = lead.thesis.review
        if review is not None:
            store.insert_review(
                conn,
                run_id,
                lead.real_thesis_id,
                review,
                _critique_body(review),
                created,
            )
        store.insert_ranking(
            conn,
            run_id,
            lead.real_id,
            lead.rank,
            lead.thesis.roi_pct,
            lead.thesis.timeframe_days,
            lead.thesis.surviving_issues,
            lead.thesis.risk_count,
            lead.rank_rationale,
            lead.presented,
            created,
        )
        if lead.real_merge_id is not None:
            store.set_merge_rank(conn, lead.real_merge_id, lead.rank)
        if lead.presented:
            store.set_status(conn, lead.real_id, "presented", lead.rank)
            store.insert_decision(conn, run_id, lead.real_id, created)
        else:
            store.set_status(conn, lead.real_id, "researched", lead.rank)
    ranked_ids = {lead.real_id for lead in ranked}
    for lead in list(leads) + list(merged):
        if lead.real_id in ranked_ids:
            continue
        status = "researched" if lead.thesis is not None else "assigned"
        store.set_status(conn, lead.real_id, status)
    store.complete_run(conn, run_id, utc_now())
    return run_id


def _thesis_from_model(data, lead, criteria, pass_number, macro):
    thesis = PlannedThesis()
    thesis.pass_number = pass_number
    thesis.criteria = criteria
    thesis.source_url = lead.source_url or ""
    grades = data.get("grades") or []
    for grade in grades:
        name = grade.get("name")
        if name:
            thesis.grades_by_name[name] = grade
    thesis.suppliers = _party_list(data.get("suppliers"))
    thesis.customers = _party_list(data.get("customers"))
    thesis.buyers = _party_list(data.get("buyers"))
    risks = [str(item).strip() for item in (data.get("risks") or []) if str(item).strip()]
    assumptions = [str(item).strip() for item in (data.get("assumptions") or []) if str(item).strip()]
    for note in _deterministic_notes(lead):
        if not any(note.casefold() in item.casefold() for item in assumptions):
            assumptions.append(note)
    thesis.risk_count = len(risks)
    thesis.risks_text = "\n".join(risks)
    thesis.assumptions_text = "\n".join(f"- {item}" for item in assumptions)
    thesis.recommendation = _text(data.get("recommendation"))
    thesis.roi = _text(data.get("roi"))
    thesis.roi_pct = _roi_pct(data)
    thesis.timeframe_days = _int_or_none(data.get("timeframe_days"))
    data = _apply_growth_evidence(data, lead, thesis)
    data = _ensure_named_parties(data, lead)
    if getattr(lead, "members", None) or pass_number == 2:
        price_run = _text(data.get("price_run"))
        thesis.price_run = price_run if price_run in _PRICE_RUNS else None
    if lead.source_scout == "capex_chain" or pass_number == 2:
        thesis.macro_context = macro or None
    thesis.markdown = _render(data, thesis, assumptions, risks)
    return thesis


def _apply_growth_evidence(data, lead, thesis):
    """Put the computed year-over-year or backlog comparison on the grade and the thesis."""
    text = _text(getattr(getattr(lead, "figure", None), "growth_text", None))
    if not text:
        return data
    for name in ("capex_growth_yoy", "backlog_qoq_growth"):
        grade = thesis.grades_by_name.get(name)
        if grade is not None and grade.get("passed"):
            grade["evidence"] = text
    body = _text(data.get("thesis"))
    if text.casefold() not in body.casefold():
        data = dict(data)
        data["thesis"] = (body + " " + text).strip()
    return data


def _ensure_named_parties(data, lead):
    """Keep the named-party sentence when the model omits it."""
    body = _text(data.get("thesis"))
    folded = body.casefold()
    extra = []
    if getattr(lead, "lead_type", "") == "capex" and "suppliers named:" not in folded:
        extra.append(_named_sentence("Suppliers named", data.get("suppliers")))
    if getattr(lead, "lead_type", "") == "orders_backlog" and "customers named:" not in folded:
        extra.append(_named_sentence("Customers named", data.get("customers")))
    if not extra:
        return data
    data = dict(data)
    data["thesis"] = (body + " " + " ".join(extra)).strip()
    return data


def _named_sentence(label, items):
    names = []
    for item in items or []:
        if isinstance(item, dict):
            name = _text(item.get("name"))
        else:
            name = _text(item)
        if name and not is_common_heading(name):
            names.append(name)
    if names:
        return f"{label}: {', '.join(names)}."
    return f"{label}: none found."


def _render(data, thesis, assumptions, risks):
    grade_lines = []
    for name, grade in thesis.grades_by_name.items():
        mark = "pass" if grade.get("passed") else "fail"
        grade_lines.append(f"- {name}: {mark}. {_text(grade.get('evidence'))}")
    assumption_lines = "\n".join(f"- {item}" for item in assumptions) or "- none stated"
    return "\n".join(
        [
            "## Grade",
            "",
            "\n".join(grade_lines),
            "",
            "## Thesis",
            "",
            _text(data.get("thesis")),
            "",
            f"Source: {thesis.source_url}",
            "",
            "## Recommendation",
            "",
            f"Where to put the money: {thesis.recommendation}",
            f"Expected return: {thesis.roi}",
            f"Risks: {'; '.join(risks)}",
            f"Timeframe: {thesis.timeframe_days} days",
            "",
            "## Assumptions",
            "",
            assumption_lines,
        ]
    )


def _components(leads):
    parent = {lead.synthetic_id: lead.synthetic_id for lead in leads}

    def find(item):
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(left, right):
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    by_id = {lead.synthetic_id: lead for lead in leads}
    ids = list(by_id)
    pair_kind = {}
    for index, left in enumerate(ids):
        for right in ids[index + 1 :]:
            kind = _link_kind(by_id[left], by_id[right])
            if not kind:
                continue
            union(left, right)
            pair_kind[frozenset((left, right))] = kind
    groups = {}
    for lead_id in ids:
        groups.setdefault(find(lead_id), []).append(by_id[lead_id])
    components = []
    for members in groups.values():
        if len(members) < 2:
            continue
        member_ids = {member.synthetic_id for member in members}
        kind = "same-company"
        for pair, linked in pair_kind.items():
            if pair <= member_ids and linked.startswith("cross-company"):
                kind = linked
                break
        components.append((kind, members))
    return components


def _link_kind(left, right):
    cross = _cross_company(left, right)
    if cross:
        return cross
    return _same_company(left, right)


def _cross_company(left, right):
    """Capex, backlog, and debt are already one lead when they link.

    A thesis written later does not form that chain again. A leftover
    merge is a cross-group pair, such as capex with insider buying.
    """
    if left.lead_type in CHAIN_TYPES and right.lead_type in CHAIN_TYPES:
        return None
    if {left.lead_type, right.lead_type} != {"capex", "orders_backlog"}:
        return None
    if left.ticker == right.ticker:
        return None
    capex = left if left.lead_type == "capex" else right
    orders = right if capex is left else left
    supplier_names, supplier_tickers = _thesis_parties(capex.thesis, "suppliers")
    order_names, order_tickers = _identity(orders)
    buyer_names, buyer_tickers = _thesis_parties(orders.thesis, "buyers")
    customer_names, customer_tickers = _thesis_parties(orders.thesis, "customers")
    buyer_names |= customer_names
    buyer_tickers |= customer_tickers
    capex_names, capex_tickers = _identity(capex)
    linked = bool(
        supplier_names & order_names
        or supplier_tickers & order_tickers
        or buyer_names & capex_names
        or buyer_tickers & capex_tickers
    )
    if not linked:
        return None
    return "cross-company capex-to-backlog"


def _same_company(left, right):
    if left.ticker != right.ticker or left.lead_type == right.lead_type:
        return None
    if left.source_url and left.source_url == right.source_url:
        return "same-company"
    left_names = _all_party_names(left.thesis)
    right_names = _all_party_names(right.thesis)
    if left_names & right_names:
        return "same-company"
    return None


def _thesis_parties(thesis, role):
    names = set()
    tickers = set()
    for item in getattr(thesis, role):
        name = item.get("name")
        if name and not is_common_heading(name):
            names.add(normalise_name(name))
        if item.get("ticker"):
            tickers.add(str(item["ticker"]).upper())
    return names, tickers


def _all_party_names(thesis):
    names = set()
    for role in ("suppliers", "customers", "buyers"):
        role_names, tickers = _thesis_parties(thesis, role)
        names |= role_names
        names |= {normalise_name(ticker) for ticker in tickers}
    return names


def _identity(lead):
    names = set()
    if lead.company:
        names.add(normalise_name(lead.company))
    if lead.ticker:
        names.add(normalise_name(lead.ticker))
    tickers = {str(lead.ticker).upper()} if lead.ticker else set()
    return names, tickers


def _merged_counterparties(members):
    found = []
    seen = set()
    for member in members:
        for party in getattr(member, "counterparties", None) or []:
            key = (party.get("role"), party.get("name_normalised"), party.get("source_url"))
            if key in seen:
                continue
            seen.add(key)
            found.append(dict(party))
    return found


def _primary(kind, members):
    if kind.startswith("cross-company"):
        for lead in members:
            if lead.lead_type == "capex":
                return lead
    return max(members, key=lambda lead: (lead.thesis.roi_pct or 0, -lead.synthetic_id))


def _rationale(kind, members):
    sentences = [f"{kind}."]
    for lead in members:
        named = _party_phrase(lead.thesis)
        sentences.append(
            f"{lead.ticker} {lead.lead_type} source {lead.source_url} names {named}."
        )
    sentences.append("A sector match is not the link.")
    return " ".join(sentences)


def _party_phrase(thesis):
    parts = []
    for role in ("suppliers", "customers", "buyers"):
        for item in getattr(thesis, role):
            label = item.get("ticker") or item.get("name")
            if label:
                parts.append(f"{role[:-1]} {label}")
    return ", ".join(parts) if parts else "no counterparty"


def _constituent_payload(lead):
    figure = lead.figure
    suppliers, customers, buyers = _named_roles(lead)
    return {
        "lead_type": lead.lead_type,
        "ticker": lead.ticker,
        "company": lead.company or "",
        "figure_type": figure.figure_type,
        "instrument": figure.instrument,
        "value": figure.value,
        "value_text": figure.value_text,
        "source_url": lead.source_url or "",
        "suppliers": suppliers,
        "customers": customers,
        "buyers": buyers,
    }


def _named_roles(lead):
    thesis = getattr(lead, "thesis", None)
    if thesis is not None:
        return list(thesis.suppliers), list(thesis.customers), list(thesis.buyers)
    suppliers, customers, buyers = [], [], []
    seen = set()

    def add(bucket, role, name, ticker):
        cleaned = _text(name)
        if not cleaned or is_common_heading(cleaned):
            return
        key = (role, cleaned.casefold())
        if key in seen:
            return
        seen.add(key)
        bucket.append({"name": cleaned, "ticker": _text(ticker)})

    for party in list(getattr(lead, "parties", None) or []) + list(getattr(lead, "counterparties", None) or []):
        role = (party.get("role") or "").casefold()
        if role == "supplier":
            add(suppliers, role, party.get("name"), party.get("ticker"))
        elif role == "customer":
            add(customers, role, party.get("name"), party.get("ticker"))
        elif role == "buyer":
            add(buyers, role, party.get("name"), party.get("ticker"))
    return suppliers, customers, buyers


def _resolve_parties(thesis, names):
    for items in (thesis.suppliers, thesis.customers, thesis.buyers):
        for item in items:
            if not item.get("ticker"):
                item["ticker"] = names.get(normalise_name(item.get("name"))) or ""


def _evidence(members, id_map):
    payload = []
    for lead in members:
        payload.append(
            {
                "lead_id": id_map[lead.synthetic_id],
                "ticker": lead.ticker,
                "lead_type": lead.lead_type,
                "source_url": lead.source_url,
                "figure_ids": list(lead.figure_ids),
            }
        )
    return payload


def _critique_body(review):
    return "\n\n".join(
        [
            "## Assumptions that fail\n\n" + review["assumptions_that_fail"],
            "## Bad or non-official sources\n\n" + review["bad_sources"],
            "## Contradicting research\n\n" + review["contradicting_research"],
            "## The other side of the claim\n\n" + review["other_side"],
            "## Price-run classification\n\n"
            + (review.get("price_run_stance") or "")
            + "\n"
            + (review.get("price_run_counterevidence") or ""),
        ]
    )


def _party_list(value):
    items = []
    for item in value or []:
        if not isinstance(item, dict):
            continue
        name = _text(item.get("name"))
        if not name or is_common_heading(name):
            continue
        items.append({"name": name, "ticker": _text(item.get("ticker"))})
    return items


def _roi_pct(data):
    raw = data.get("roi_pct")
    if raw is None or raw == "":
        text = str(data.get("roi") or "").replace("%", "").strip()
        raw = text
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0


def _int_or_none(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _text(value):
    if value is None:
        return ""
    return " ".join(str(value).split())


def _plain(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)
