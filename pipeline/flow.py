"""Sweep latest scout figures into pending decisions.

Stages, in order: thresholds and dedupe, significance inside the token
budget, one Centurion thesis per lead, Legate merge of linked theses,
a second Centurion pass only for each merged lead, Praetorian critique,
Legate ranking, and a pending decision for each presented lead.

The merge decision is deterministic. It uses the suppliers, customers, and
buyers named in the theses plus the source URLs. A shared sector is not a
link. The return pass classifies the price run as new, acceleration, or
priced-in, and includes macro text only when a macro cache is present.
"""

import json

from shared.pipeline_store import connect
from shared.shares import cache_shares_outstanding, companies_from_figures
from shared.store import utc_now

from pipeline.budget import TokenBudget
from pipeline.llm import parse_json_object, resolve_provider
from pipeline.load import load_latest_figures
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
)
from pipeline.prompts import (
    centurion_prompt,
    load_macro,
    ordered_types,
    praetorian_prompt,
    rules_text,
    significance_prompt,
)
from pipeline import store

SHORTLIST_MAX = 5
_MAX_SIGNIFICANCE = 300
_MAX_CENTURION = 800
_MAX_PRAETORIAN = 500
_PRICE_RUNS = {"new", "acceleration", "priced-in"}
_LEAD_TYPES = ("capex", "debt_raise", "orders_backlog", "accumulation", "insider_buying")


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


def run_pipeline(
    db_path,
    *,
    dry_run=False,
    limit=None,
    token_budget=80000,
    provider=None,
    macro_cache=None,
    now=None,
    shares_client=None,
):
    """Run one sweep. `dry_run` computes the result and writes nothing."""
    if limit is not None and int(limit) < 1:
        raise ValueError("limit must be a positive integer")
    for lead_type in _LEAD_TYPES:
        rules_text(lead_type)
    provider = provider or resolve_provider()
    budget = TokenBudget(token_budget)
    started = now or utc_now()
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
        candidates, rejects, used = prefilter(conn, figures, limit=limit, now=started)
        leads = _rate(conn, provider, budget, candidates, rejects)
        _research(conn, provider, budget, leads, names, macro)
        leads = _drop_hard_failures(leads, rejects)
        merged = _merge(conn, provider, budget, leads, macro)
        merged = _drop_hard_failures(merged, rejects)
        final = _final_leads(leads, merged)
        _critique(provider, budget, final)
        ranked = _rank(final)
        presented = [lead for lead in ranked if lead.presented]
        run_id = None
        if not dry_run:
            run_id = _persist(conn, started, budget, provider.name, rejects, leads, merged, ranked)
    finally:
        conn.close()
    return {
        "run_id": run_id,
        "provider": provider.name,
        "tokens_used": budget.used,
        "token_budget": budget.limit,
        "figures": used,
        "leads": len(leads) + len(merged),
        "rejects": len(rejects),
        "merges": len(merged),
        "rankings": len(ranked),
        "decisions": len(presented),
        "dry_run": bool(dry_run),
        "macro": _macro_log(macro, merged),
    }


def _macro_log(macro, merged):
    if not macro:
        return "skipped"
    second_pass = sum(
        1
        for lead in merged
        if getattr(lead, "thesis", None) is not None and lead.thesis.pass_number == 2
    )
    return f"loaded (second-pass theses: {second_pass})"


def _rate(conn, provider, budget, candidates, rejects):
    leads = []
    next_id = 1
    for candidate in candidates:
        prompt = significance_prompt(candidate)
        data = _complete(provider, budget, prompt, _MAX_SIGNIFICANCE)
        if data is None:
            rejects.append(
                PlannedReject(
                    candidate.figure,
                    candidate.lead_type,
                    "llm",
                    "token budget exhausted",
                    parties=candidate.parties,
                )
            )
            continue
        rating = Rating.from_model(data)
        if not interesting(rating):
            rejects.append(
                PlannedReject(
                    candidate.figure,
                    candidate.lead_type,
                    "llm",
                    failure_reason(rating),
                    rating,
                    candidate.parties,
                )
            )
            continue
        lead = _lead_from_candidate(candidate, rating)
        lead.synthetic_id = next_id
        next_id += 1
        lead.parent_position_id = store.open_position_id(conn, lead.ticker)
        leads.append(lead)
    return leads


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
    lead.figure_ids = [figure.figure_id]
    lead.figure = figure
    lead.rating = rating
    lead.grade_types = [candidate.lead_type]
    lead.parties = list(candidate.parties)
    lead.market_cap_unverified = bool(getattr(figure, "market_cap_unverified", False))
    lead.growth_unverified = bool(getattr(figure, "growth_unverified", False))
    return lead


def _research(conn, provider, budget, leads, names, macro):
    for lead in leads:
        criteria = store.criteria_for(conn, lead.grade_types)
        prompt = centurion_prompt(lead, criteria, 1, macro=macro)
        data = _complete(provider, budget, prompt, _MAX_CENTURION)
        if data is None:
            continue
        lead.thesis = _thesis_from_model(data, lead, criteria, 1, macro=None)
        _resolve_parties(lead.thesis, names)


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
                "llm",
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


def _merge(conn, provider, budget, leads, macro):
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
        criteria = store.criteria_for(conn, lead.grade_types)
        constituents = [_constituent_payload(member) for member in members]
        prompt = centurion_prompt(lead, criteria, 2, constituents=constituents, macro=macro)
        data = _complete(provider, budget, prompt, _MAX_CENTURION)
        if data is not None:
            lead.thesis = _thesis_from_model(data, lead, criteria, 2, macro=macro)
        merged.append(lead)
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


def _critique(provider, budget, final):
    for lead in final:
        prompt = praetorian_prompt(lead.thesis.markdown, lead.thesis.price_run)
        data = _complete(provider, budget, prompt, _MAX_PRAETORIAN)
        if data is None:
            continue
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
        lead.thesis.review = review
        lead.thesis.surviving_issues = review["surviving_issues"]


def _note_unverified_cap(lead, review):
    if not getattr(lead, "market_cap_unverified", False):
        return
    if UNVERIFIED in review["assumptions_that_fail"].casefold():
        return
    review["assumptions_that_fail"] = (review["assumptions_that_fail"] + " " + UNVERIFIED).strip()
    review["surviving_issues"] += 1


def _rank(final):
    reviewed = [lead for lead in final if lead.thesis is not None and lead.thesis.review is not None]
    reviewed.sort(
        key=lambda lead: (
            lead.thesis.surviving_issues,
            -float(lead.thesis.roi_pct or 0),
            lead.thesis.timeframe_days if lead.thesis.timeframe_days is not None else 10**9,
            lead.thesis.risk_count,
            lead.synthetic_id,
        )
    )
    for index, lead in enumerate(reviewed, start=1):
        lead.rank = index
        lead.presented = index <= SHORTLIST_MAX
        lead.rank_rationale = (
            f"Rank {index}: {lead.thesis.surviving_issues} surviving issues, "
            f"ROI {lead.thesis.roi or lead.thesis.roi_pct}, "
            f"timeframe {lead.thesis.timeframe_days} days, "
            f"{lead.thesis.risk_count} risks."
        )
    return reviewed


def _persist(conn, started, budget, provider_name, rejects, leads, merged, ranked):
    run_id = store.start_run(conn, started)
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
    store.finish_run(conn, run_id, budget.limit, budget.used, provider_name, started, utc_now())
    return run_id


def _complete(provider, budget, prompt, max_tokens):
    estimate = budget.estimate(prompt, max_tokens)
    if not budget.can_spend(estimate):
        return None
    result = provider.complete(prompt, max_tokens=max_tokens)
    budget.charge(result.prompt_tokens, result.completion_tokens)
    return parse_json_object(result.text)


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
    if getattr(lead, "market_cap_unverified", False):
        if not any(UNVERIFIED in item.casefold() for item in assumptions):
            assumptions.append(UNVERIFIED)
    thesis.risk_count = len(risks)
    thesis.risks_text = "\n".join(risks)
    thesis.assumptions_text = "\n".join(f"- {item}" for item in assumptions)
    thesis.recommendation = _text(data.get("recommendation"))
    thesis.roi = _text(data.get("roi"))
    thesis.roi_pct = _roi_pct(data)
    thesis.timeframe_days = _int_or_none(data.get("timeframe_days"))
    if pass_number == 2:
        price_run = _text(data.get("price_run"))
        thesis.price_run = price_run if price_run in _PRICE_RUNS else None
        thesis.macro_context = macro or None
    thesis.markdown = _render(data, thesis, assumptions, risks)
    return thesis


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
        if item.get("name"):
            names.add(normalise_name(item["name"]))
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
    return {
        "lead_type": lead.lead_type,
        "ticker": lead.ticker,
        "company": lead.company or "",
        "figure_type": figure.figure_type,
        "instrument": figure.instrument,
        "value": figure.value,
        "value_text": figure.value_text,
        "source_url": lead.source_url or "",
        "suppliers": lead.thesis.suppliers,
        "customers": lead.thesis.customers,
        "buyers": lead.thesis.buyers,
    }


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
        if not name:
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
