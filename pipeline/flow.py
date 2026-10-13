"""Sweep latest scout figures into thesis files.

`run_pipeline` does the deterministic work, writes significance task files,
records status awaiting_agents, and returns. `ingest_run` checks result
files and advances one judgement stage. Combining leads and a leftover Legate
merge stay in process. Ranking writes the candidate report and one legate task.

Stages: prices (US quotes only, when the scout gate is waiting on a price),
significance, then one Centurion task per lead, a leftover Legate merge,
centurion pass 2 only for that leftover, then a Praetorian critique,
then ranking, then Legate selection. The scout gate applies the fixed
rubric before significance. A failed candidate is stored as filtered and
is not queued. A missing or stale price is stored as needs_price. The
critique lists every flaw and does not drop a lead. Ranking writes every
candidate thesis to `data/reports/<run_id>-candidates.md` and a
legate_selection task. The Legate selects at most 5. Ingest writes one
file per selected thesis under `data/reports/<run_id>/<rank>-<ticker>.md`.
Linkable capex, orders backlog, and debt raises become one capex-chain
lead before that Centurion, after each component has passed its own
rubric. Same-ticker insider and accumulation signals inside the window
become one smart-money lead the same way. Macro stays a lead of its own
and has no rubric gate. ASX prices are fetched from Markit before the
gate. A US price comes from the prices handoff.

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
    LEGATE_SELECTION_SCHEMA,
    LEGATE_TASK_ID,
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
from pipeline.prices import (
    considered_figures,
    ingest_price_result,
    prices_outstanding,
    refresh_markit,
    write_needed,
)
from pipeline.rubric_gate import evaluate
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
    legate_selection_prompt,
    praetorian_prompt,
    rules_text,
    significance_prompt,
)
from pipeline.report import reports_directory, write_candidates_report, write_selected_reports
from pipeline.scout_groups import CHAIN_TYPES, combine
from pipeline import store

# One surviving issue costs this many score points. Magnitude and growth
# can still place a large backlog or a sharp capex print above a small stake.
_ISSUE_PENALTY = 8.0
_PRICE_RUNS = {"new", "acceleration", "priced-in"}
_LEAD_TYPES = ("capex", "debt_raise", "orders_backlog", "accumulation", "insider_buying", "macro")
_AGENT_STAGES = ("significance", "centurion_1", "centurion_2", "praetorian", "legate_selection")
_LEGATE_RULES = (
    "Select at most 5 candidate lead ids. "
    "Each pick has a rationale. "
    "Each lead id is copied from the candidate list."
)
_JUDGEMENT_STAGE = "agent"
PRAETORIAN_RULES = Path(__file__).resolve().parents[1] / "Praetorians" / "README.md"


class PlannedThesis:
    def __init__(self):
        self.markdown = ""
        self.risks_text = ""
        self.assumptions_text = ""
        self.recommendation = ""
        self.conviction = ""
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
        self.pick_order = None
        self.selection_rationale = None
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
    markit_client=None,
    queue_dir=None,
):
    """Do the deterministic work and queue the next agent stage.

    ASX prices are fetched from Markit when a client is passed. The scout
    gate then runs. A candidate that fails a rubric criterion is stored as
    filtered and is not queued. A candidate that still needs an official
    price is stored as needs_price. US names in that state are written to
    prices/needed.json and the run stops there. Significance tasks are
    written once those quotes are ingested, or immediately when no US
    candidate is waiting on a price.
    Counterparties are named later, on ingest, and only when that call is
    given a parties client. This function does not judge a lead and does not
    call a model.
    """
    if limit is not None and int(limit) < 1:
        raise ValueError("limit must be a positive integer")
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
            queued = 0
            for candidate in candidates:
                if evaluate(conn, candidate.figure, candidate.lead_type, now=started).status != "filtered":
                    queued += 1
            for candidate in macro_candidates(conn, started):
                if not _in_flight(conn, candidate):
                    queued += 1
            return _awaiting_result(
                SweepState(),
                status="dry_run",
                tasks=queued,
                figures=used,
                macro=macro,
                dry_run=True,
            )
        considered = considered_figures(figures, limit)
        refresh_markit(conn, considered, markit_client, started)
        state = SweepState()
        state.started = started
        state.macro = macro
        state.names = names
        state.queue_dir = queue
        state.reports_dir = reports_directory(queue)
        state.limit = limit
        run_id = store.start_run(conn, started)
        state.run_id = run_id
        _screen(conn, state)
        needed = _needs_price_tickers(state)
        if needed:
            write_needed(queue, run_id, needed, started)
            state.stage = "prices"
            store.begin_pipeline_run(conn, run_id, started, queue, "awaiting_agents", "prices")
            save_state(state)
            return _awaiting_result(
                state, status="awaiting_agents", tasks=len(needed), figures=state.figures, macro=macro
            )
        store.begin_pipeline_run(conn, run_id, started, queue, "awaiting_agents", "significance")
        tasks = _open_significance(conn, state)
        if state.finished:
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
    are ready. Ranking writes the candidate report and one legate task.
    The selection result then writes one file per selected thesis.
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
            if _candidates_report_path(state) is None:
                write_candidates_report(state, _ranked_from_state(state), conn)
            if _selected_report_dir(state) is None:
                write_selected_reports(state, _selected_from_state(state), conn)
            return _complete_result(conn, state)
        state = load_state(queue, run_id)
        if state.pending_persist:
            _finish(conn, state, leads=state.leads, merged=state.merged, ranked=_ranked_from_state(state))
            return _complete_result(conn, state)
        stage = state.stage
        if stage == "prices":
            ingest_price_result(conn, queue, run_id, state.started)
            _promote_needs_price(conn, state)
            tasks = _open_significance(conn, state)
            if state.stage == "ranking":
                return _complete_result(conn, state)
            save_state(state)
            store.set_pipeline_stage(conn, run_id, "awaiting_agents", state.stage)
            return _awaiting_result(
                state, status="awaiting_agents", tasks=tasks, figures=state.figures, macro=state.macro
            )
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
            elif stage == "legate_selection":
                _apply_legate_selection(conn, state, results)
        if state.pending_persist:
            _finish(conn, state, leads=state.leads, merged=state.merged, ranked=_ranked_from_state(state))
            return _complete_result(conn, state)
        if state.finished:
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
        if row["finished_at"]:
            pending = []
        elif stage == "prices":
            pending = prices_outstanding(queue, run_id)
        elif stage not in _AGENT_STAGES:
            pending = []
        else:
            pending = outstanding_tasks(queue, run_id, stage)
    finally:
        conn.close()
    outstanding = []
    for path in pending:
        if stage == "prices":
            lead_id = "prices"
        else:
            lead_id = path.name[: -len(".task.json")]
        outstanding.append({"lead_id": lead_id, "stage": stage, "task": str(path)})
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
        "dry_run": bool(dry_run),
        "macro": _macro_log(macro, []),
        "status": status,
        "stage": "significance" if dry_run else state.stage,
        "tasks": tasks,
        "queue_dir": None if dry_run else state.queue_dir,
        "report": None,
        "candidates": None,
    }


def _complete_result(conn, state):
    run_id = state.run_id
    leads = conn.execute("SELECT COUNT(*) FROM leads WHERE run_id = ?", (run_id,)).fetchone()[0]
    rejects = conn.execute("SELECT COUNT(*) FROM lead_rejects WHERE run_id = ?", (run_id,)).fetchone()[0]
    merges = conn.execute("SELECT COUNT(*) FROM legate_merges WHERE run_id = ?", (run_id,)).fetchone()[0]
    rankings = conn.execute("SELECT COUNT(*) FROM rankings WHERE run_id = ?", (run_id,)).fetchone()[0]
    second = conn.execute(
        """
        SELECT COUNT(*) FROM theses
        WHERE run_id = ? AND macro_context IS NOT NULL AND length(trim(macro_context)) > 0
        """,
        (run_id,),
    ).fetchone()[0]
    stage = conn.execute("SELECT stage FROM pipeline_runs WHERE run_id = ?", (run_id,)).fetchone()
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
        "dry_run": False,
        "macro": macro,
        "status": "complete",
        "stage": stage["stage"] if stage else "legate_selection",
        "tasks": 0,
        "queue_dir": state.queue_dir,
        "report": _selected_report_dir(state),
        "candidates": _candidates_report_path(state),
    }


def _report_file(state, suffix):
    directory = getattr(state, "reports_dir", None) or reports_directory(state.queue_dir or "data/agent_queue")
    path = Path(directory) / f"{state.run_id}{suffix}"
    if path.is_file():
        return str(path)
    return None


def _selected_report_dir(state):
    directory = Path(
        getattr(state, "reports_dir", None) or reports_directory(state.queue_dir or "data/agent_queue")
    ) / str(state.run_id)
    if directory.is_dir() and any(directory.glob("*.md")):
        return str(directory)
    return None


def _candidates_report_path(state):
    return _report_file(state, "-candidates.md")


def _ranked_from_state(state):
    by_id = {lead.synthetic_id: lead for lead in list(state.leads) + list(state.merged)}
    final = [by_id[item] for item in state.final_ids]
    ranked = [lead for lead in final if getattr(lead, "rank", None)]
    ranked.sort(key=lambda lead: lead.rank or 0)
    return ranked


def _selected_from_state(state):
    selected = [lead for lead in _ranked_from_state(state) if getattr(lead, "pick_order", None)]
    selected.sort(key=lambda lead: lead.pick_order)
    return selected


def _screen(conn, state):
    """Apply the rubric gate once. Filtered and needs_price leads are stored."""
    if state.screened:
        return
    figures = load_latest_figures(conn, as_of=state.started)
    candidates, rejects, used = prefilter(conn, figures, limit=state.limit, now=state.started)
    state.figures = used
    state.deterministic_rejects = len(rejects)
    created = utc_now()
    for planned in rejects:
        store.insert_reject(conn, state.run_id, planned, created)
    passed = []
    waiting = []
    for candidate in candidates:
        outcome = evaluate(conn, candidate.figure, candidate.lead_type, now=state.started)
        candidate.gate_results = outcome.results
        existing = store.screened_lead_for_figure(conn, candidate.figure.figure_id)
        if existing is not None and existing["status"] == "filtered" and outcome.status == "filtered":
            continue
        if existing is not None and outcome.status == "pass":
            store.delete_screened_lead(conn, existing["lead_id"])
            passed.append(candidate)
            continue
        if existing is not None:
            store.update_screened_lead(
                conn,
                existing["lead_id"],
                outcome,
                state.run_id,
                candidate.figure.source_url or None,
                created,
            )
            candidate.screen_lead_id = existing["lead_id"]
            if outcome.status == "filtered":
                _record_filter(conn, state, candidate, outcome, created)
            else:
                waiting.append(candidate)
            continue
        if outcome.status == "pass":
            passed.append(candidate)
            continue
        lead_id = store.insert_screened_lead(conn, state.run_id, candidate, outcome, created)
        candidate.screen_lead_id = lead_id
        if outcome.status == "filtered":
            _record_filter(conn, state, candidate, outcome, created)
        else:
            waiting.append(candidate)
    for candidate in macro_candidates(conn, state.started):
        if _in_flight(conn, candidate):
            continue
        candidate.gate_results = []
        passed.append(candidate)
    state.candidates = passed
    state.waiting = waiting
    state.screened = True


def _promote_needs_price(conn, state):
    """Re-gate leads that were waiting on a price. A pass joins the queue."""
    created = utc_now()
    still = []
    for candidate in list(state.waiting or []):
        outcome = evaluate(conn, candidate.figure, candidate.lead_type, now=state.started)
        candidate.gate_results = outcome.results
        lead_id = getattr(candidate, "screen_lead_id", None)
        if outcome.status == "pass":
            if lead_id is not None:
                store.delete_screened_lead(conn, lead_id)
            candidate.screen_lead_id = None
            state.candidates.append(candidate)
            continue
        if lead_id is not None:
            store.update_screened_lead(
                conn,
                lead_id,
                outcome,
                state.run_id,
                candidate.figure.source_url or None,
                created,
            )
        if outcome.status == "filtered":
            _record_filter(conn, state, candidate, outcome, created)
            continue
        still.append(candidate)
    state.waiting = still
    needed = _needs_price_tickers(state)
    if needed and state.queue_dir and state.run_id:
        write_needed(state.queue_dir, state.run_id, needed, state.started)


def _record_filter(conn, state, candidate, outcome, created):
    reason = "; ".join(
        f"{item['name']}: {item.get('evidence') or 'fail'}"
        for item in outcome.results
        if not item.get("passed") and not item.get("needs_price")
    ) or "filtered"
    store.insert_reject(
        conn,
        state.run_id,
        PlannedReject(
            candidate.figure,
            candidate.lead_type,
            "deterministic",
            reason,
            Rating(magnitude="small", magnitude_rationale=reason),
            candidate.parties,
        ),
        created,
    )
    state.deterministic_rejects += 1


def _needs_price_tickers(state):
    from pipeline.load import exchange_for

    needed = []
    seen = set()
    for candidate in state.waiting or []:
        figure = candidate.figure
        ticker = figure.ticker
        if not ticker or ticker in seen or exchange_for(figure) == "ASX":
            continue
        seen.add(ticker)
        needed.append({"exchange": "US", "ticker": ticker})
    needed.sort(key=lambda item: item["ticker"])
    return needed


def _open_significance(conn, state):
    """Queue significance for candidates that passed the scout gate."""
    if not state.candidates:
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
                "gate_results": list(getattr(lead, "gate_results", None) or []),
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
    _publish_candidates(conn, state)


def _queue_praetorian(conn, state):
    final = _final_leads(state.leads, state.merged)
    state.final_ids = [lead.synthetic_id for lead in final]
    if not final:
        _queue_ranking(conn, state)
        return
    _write_praetorian_tasks(state, final)
    state.stage = "praetorian"


def _queue_ranking(conn, state):
    final = _final_leads(state.leads, state.merged)
    state.final_ids = [lead.synthetic_id for lead in final]
    _rank(final)
    state.pending_persist = True
    state.stage = "ranking"
    save_state(state)
    _ = conn


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
    lead.gate_results = list(getattr(candidate, "gate_results", None) or [])
    lead.market_cap_unverified = False
    lead.growth_unverified = False
    return lead


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
        lead.market_cap_unverified = False
        lead.growth_unverified = False
        gate_results = []
        for member in members:
            for item in getattr(member, "gate_results", None) or []:
                gate_results.append(item)
        lead.gate_results = gate_results
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
    """Store the critique. The flaw list does not change which leads continue."""
    flaws = []
    for item in data.get("flaws") or []:
        flaws.append(
            {
                "flaw": _text(item.get("flaw")),
                "severity": _text(item.get("severity")).casefold(),
                "evidence": _text(item.get("evidence")),
            }
        )
    lead.thesis.review = {"flaws": flaws}


def _rank(final):
    reviewed = []
    for lead in final:
        if lead.thesis is None:
            continue
        lead.thesis.surviving_issues = _issues_without_praetorian(lead)
        reviewed.append(lead)
    scored = [( _rank_score(lead), lead) for lead in reviewed]
    scored.sort(key=lambda item: _rank_key(item[1], item[0]))
    for index, (score, lead) in enumerate(scored, start=1):
        lead.rank = index
        lead.presented = False
        lead.pick_order = None
        lead.selection_rationale = None
        flaws = (getattr(lead.thesis, "review", None) or {}).get("flaws") or []
        lead.rank_rationale = (
            f"Rank {index}: score {score:.1f} from ROI {lead.thesis.roi or lead.thesis.roi_pct}, "
            f"growth and magnitude, minus {lead.thesis.surviving_issues} surviving issues; "
            f"timeframe {lead.thesis.timeframe_days} days, "
            f"{lead.thesis.risk_count} risks. "
            f"Praetorian flaws recorded: {len(flaws)}."
        )
    return [lead for _score, lead in scored]


def _publish_candidates(conn, state):
    """Store every ranked thesis and queue the Legate selection."""
    by_id = {lead.synthetic_id: lead for lead in list(state.leads) + list(state.merged)}
    final = [by_id[item] for item in state.final_ids]
    ranked = _rank(final)
    _persist(conn, state.run_id, state.judgement_rejects, state.leads, state.merged, ranked)
    write_candidates_report(state, ranked, conn)
    if not ranked:
        write_selected_reports(state, [], conn)
        store.complete_run(conn, state.run_id, utc_now())
        state.pending_persist = False
        state.finished = True
        state.stage = "legate_selection"
        save_state(state)
        return
    _write_legate_task(state, ranked)
    state.pending_persist = False
    state.finished = False
    state.stage = "legate_selection"
    save_state(state)


def _write_legate_task(state, ranked):
    candidates = []
    for lead in ranked:
        flaws = (getattr(lead.thesis, "review", None) or {}).get("flaws") or []
        candidates.append(
            {
                "lead_id": lead.real_id,
                "ticker": lead.ticker,
                "rank": lead.rank,
                "source_scout": lead.source_scout,
                "summary": lead.summary,
                "flaws": len(flaws),
            }
        )
    directory = state.reports_dir or reports_directory(state.queue_dir)
    report = str(Path(directory) / f"{state.run_id}-candidates.md")
    _write_judgement_task(
        state,
        "legate_selection",
        LEGATE_TASK_ID,
        legate_selection_prompt(candidates),
        _LEGATE_RULES,
        {"run_id": state.run_id},
        {"candidates_report": report, "candidates": candidates},
        None,
        LEGATE_SELECTION_SCHEMA,
    )


def _apply_legate_selection(conn, state, results):
    data = results.get(LEGATE_TASK_ID)
    if data is None:
        raise AgentResultError(f"{LEGATE_TASK_ID}: missing result")
    known = {lead.real_id: lead for lead in _ranked_from_state(state) if lead.real_id is not None}
    db_ids = {
        row["lead_id"]
        for row in conn.execute("SELECT lead_id FROM rankings WHERE run_id = ?", (state.run_id,))
    }
    unknown = [item for item in data["lead_ids"] if item not in db_ids or item not in known]
    if unknown:
        listed = ", ".join(str(item) for item in unknown)
        raise AgentResultError(f"{LEGATE_TASK_ID}: unknown lead_ids {listed}")
    picks = []
    selected = []
    for order, lead_id in enumerate(data["lead_ids"], start=1):
        lead = known[lead_id]
        rationale = str(data["rationale"][str(lead_id)]).strip()
        lead.presented = True
        lead.pick_order = order
        lead.selection_rationale = rationale
        picks.append((lead_id, rationale))
        selected.append(lead)
    created = utc_now()
    store.record_selection(conn, state.run_id, picks, created)
    write_selected_reports(state, selected, conn)
    state.pending_persist = False
    state.finished = True
    state.stage = "legate_selection"
    save_state(state)


def _deterministic_notes(lead):
    """Soft significance notes recorded on the thesis. Praetorian flaws are not in this list."""
    notes = []
    issue = getattr(lead, "significance_issue", None)
    if issue:
        notes.append(issue)
    return notes


def _issues_without_praetorian(lead):
    """Soft gates. A Praetorian flaw does not add to this count."""
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
    """Finish a sweep that has no candidate theses."""
    row = store.pipeline_run(conn, state.run_id)
    if row is None or row["finished_at"] is None:
        _persist(conn, state.run_id, state.judgement_rejects, leads, merged, ranked)
        write_candidates_report(state, ranked, conn)
        write_selected_reports(state, [], conn)
        store.complete_run(conn, state.run_id, utc_now())
    state.pending_persist = False
    state.finished = True
    state.stage = "legate_selection"
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
        store.record_gate_results(
            conn, lead.real_id, getattr(lead, "gate_results", None), run_id, lead.source_url, created
        )
    for lead in merged:
        lead.real_id = store.insert_lead(conn, run_id, lead, created)
        id_map[lead.synthetic_id] = lead.real_id
        if lead.thesis is not None:
            lead.real_thesis_id = store.insert_thesis(conn, run_id, lead.real_id, lead.thesis, created)
        store.record_gate_results(
            conn, lead.real_id, getattr(lead, "gate_results", None), run_id, lead.source_url, created
        )
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
        store.set_status(conn, lead.real_id, "researched", lead.rank)
    ranked_ids = {lead.real_id for lead in ranked}
    for lead in list(leads) + list(merged):
        if lead.real_id in ranked_ids:
            continue
        status = "researched" if lead.thesis is not None else "assigned"
        store.set_status(conn, lead.real_id, status)
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
    thesis.conviction = _text(data.get("conviction"))
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


def _paragraphs(value):
    text = str(value or "").replace("\r\n", "\n").strip()
    blocks = []
    current = []
    for line in text.split("\n"):
        if line.strip():
            current.append(line.strip())
            continue
        if current:
            blocks.append(" ".join(" ".join(current).split()))
            current = []
    if current:
        blocks.append(" ".join(" ".join(current).split()))
    return "\n\n".join(blocks)


def _render(data, thesis, assumptions, risks):
    assumption_lines = "\n".join(f"- {item}" for item in assumptions) or "- none stated"
    return "\n".join(
        [
            "## Thesis",
            "",
            _paragraphs(data.get("thesis")),
            "",
            f"Source: {thesis.source_url}",
            "",
            "## Recommendation",
            "",
            f"Recommendation: {thesis.recommendation}",
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
    flaws = review.get("flaws") or []
    if not flaws:
        return "No flaws recorded."
    lines = ["## Flaws", ""]
    for item in flaws:
        lines.append(f"- {item['severity']}: {item['flaw']}")
        lines.append(f"  Evidence: {item['evidence']}")
    return "\n".join(lines)


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
