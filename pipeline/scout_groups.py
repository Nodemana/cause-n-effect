"""Combine scout leads into one finding before the Centurion.

Data-collection scouts stay separate modules. This step runs after
significance, once parties are known, and replaces the separate leads
when a link exists. An unlinked figure stays a single lead. Macro leads
stay on their own.
"""

from pipeline.counterparties import is_common_heading
from pipeline.load import event_date
from pipeline.names import normalise_name
from pipeline.prefilter import ACCUMULATION_WINDOW_DAYS
from pipeline.prompts import ordered_types

CHAIN_TYPES = ("capex", "orders_backlog", "debt_raise")
SMART_TYPES = ("insider_buying", "accumulation")
_CHAIN_RANK = {"capex": 0, "orders_backlog": 1, "debt_raise": 2}
_CHAIN_LABEL = {
    "capex": "Spender",
    "orders_backlog": "Supplier (backlog)",
    "debt_raise": "Funding",
}


def combine(leads):
    """Return chain leads, smart-money leads, and the leads that stay single."""
    from pipeline.flow import PlannedLead

    consumed = set()
    grouped = {}
    next_id = 1 + max((lead.synthetic_id or 0 for lead in leads), default=0)
    chain_pool = [
        lead for lead in leads if lead.parent_position_id is None and lead.lead_type in CHAIN_TYPES
    ]
    for members in _clusters(chain_pool, _chain_linked):
        if len(members) < 2:
            continue
        lead = _chain_lead(PlannedLead, _ordered(members), next_id)
        next_id += 1
        for member in members:
            consumed.add(id(member))
            grouped[id(member)] = lead
    smart_pool = [
        lead for lead in leads if lead.parent_position_id is None and lead.lead_type in SMART_TYPES
    ]
    for members in _clusters(smart_pool, _smart_linked):
        if len(members) < 2:
            continue
        lead = _smart_lead(PlannedLead, members, next_id)
        next_id += 1
        for member in members:
            consumed.add(id(member))
            grouped[id(member)] = lead
    result = []
    emitted = set()
    for lead in leads:
        if id(lead) in consumed:
            group = grouped[id(lead)]
            if id(group) in emitted:
                continue
            emitted.add(id(group))
            result.append(group)
            continue
        result.append(lead)
    attach_related(result)
    return result


def attach_related(leads):
    """Attach chain and smart-money findings that match a macro series."""
    rules = _relevance_rules()
    others = [lead for lead in leads if lead.lead_type != "macro"]
    for lead in leads:
        if lead.lead_type != "macro":
            lead.related_findings = []
            continue
        series_id = (lead.ticker or "").strip().upper()
        keywords = rules.get(series_id) or [series_id.casefold()]
        words = [word.casefold() for word in keywords if word]
        found = []
        for other in others:
            if any(word in _blob(other) for word in words):
                found.append(_related_payload(other))
        lead.related_findings = found


def _clusters(leads, linked):
    parent = {id(lead): id(lead) for lead in leads}

    def find(item):
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(left, right):
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    for index, left in enumerate(leads):
        for right in leads[index + 1 :]:
            if linked(left, right):
                union(id(left), id(right))
    groups = {}
    for lead in leads:
        groups.setdefault(find(id(lead)), []).append(lead)
    return list(groups.values())


def _chain_linked(left, right):
    if left.ticker and left.ticker == right.ticker:
        return True
    types = {left.lead_type, right.lead_type}
    if types != {"capex", "orders_backlog"}:
        return False
    if left.ticker == right.ticker:
        return False
    capex = left if left.lead_type == "capex" else right
    orders = right if capex is left else left
    supplier_names, supplier_tickers = _role_keys(capex, {"supplier"})
    order_names, order_tickers = _identity(orders)
    buyer_names, buyer_tickers = _role_keys(orders, {"buyer", "customer"})
    capex_names, capex_tickers = _identity(capex)
    return bool(
        supplier_names & order_names
        or supplier_tickers & order_tickers
        or buyer_names & capex_names
        or buyer_tickers & capex_tickers
    )


def _smart_linked(left, right):
    if not left.ticker or left.ticker != right.ticker:
        return False
    left_day = event_date(left.figure) if left.figure is not None else None
    right_day = event_date(right.figure) if right.figure is not None else None
    if left_day is None or right_day is None:
        return False
    return abs((left_day - right_day).days) <= ACCUMULATION_WINDOW_DAYS


def _chain_lead(planned, members, synthetic_id):
    primary = _chain_primary(members)
    lead = planned()
    _fill_group(lead, primary, members, synthetic_id)
    lead.lead_type = primary.lead_type if primary.lead_type in CHAIN_TYPES else "capex"
    if any(member.lead_type == "capex" for member in members):
        lead.lead_type = "capex"
    elif any(member.lead_type == "orders_backlog" for member in members):
        lead.lead_type = "orders_backlog"
    else:
        lead.lead_type = "debt_raise"
    lead.source_scout = "capex_chain"
    lead.link_kind = "capex-chain"
    lead.summary = _chain_summary(members)
    return lead


def _chain_primary(members):
    for lead_type in ("capex", "orders_backlog", "debt_raise"):
        for member in members:
            if member.lead_type == lead_type:
                return member
    return members[0]


def _smart_lead(planned, members, synthetic_id):
    ordered = sorted(members, key=lambda lead: (0 if lead.lead_type == "insider_buying" else 1, lead.synthetic_id or 0))
    primary = ordered[0]
    lead = planned()
    _fill_group(lead, primary, ordered, synthetic_id)
    lead.lead_type = "insider_buying" if any(member.lead_type == "insider_buying" for member in ordered) else "accumulation"
    lead.source_scout = "smart_money"
    lead.link_kind = "smart-money"
    lead.summary = _smart_summary(ordered)
    return lead


def _fill_group(lead, primary, members, synthetic_id):
    lead.synthetic_id = synthetic_id
    lead.ticker = primary.ticker
    lead.exchange = primary.exchange
    lead.company = primary.company
    lead.source_url = primary.source_url
    lead.figure = primary.figure
    lead.figure_ids = _figure_ids(members)
    lead.rating = primary.rating
    lead.grade_types = ordered_types(member.lead_type for member in members)
    lead.members = list(members)
    lead.parties = _merged_named(members, "parties")
    lead.counterparties = _merged_named(members, "counterparties")
    lead.parent_position_id = None
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
    lead.related_findings = []


def _ordered(members):
    return sorted(members, key=lambda lead: (_CHAIN_RANK.get(lead.lead_type, 9), lead.synthetic_id or 0))


def _chain_summary(members):
    parts = []
    for member in members:
        label = _CHAIN_LABEL.get(member.lead_type, member.lead_type)
        company = member.company or ""
        parts.append(f"{label} {member.ticker} {company} source {member.source_url}.")
    return " ".join(parts)


def _smart_summary(members):
    parts = []
    for member in members:
        figure = member.figure
        detail = " ".join(
            bit
            for bit in (
                getattr(figure, "instrument", None),
                getattr(figure, "period", None),
                getattr(figure, "value_text", None),
            )
            if bit
        )
        parts.append(f"{_signal_label(member)} {member.ticker} {detail} source {member.source_url}.")
    return " ".join(parts)


def _signal_label(member):
    if member.lead_type == "insider_buying":
        return "Form 4"
    figure = member.figure
    blob = " ".join(
        str(bit or "")
        for bit in (
            getattr(figure, "instrument", None),
            getattr(figure, "period", None),
            getattr(figure, "value_text", None),
            getattr(figure, "source_type", None),
            member.source_url,
        )
    ).casefold()
    if "604" in blob or "asx.com" in blob or "asx announcement" in blob:
        return "ASX 604"
    return "13D/13G"


def _figure_ids(members):
    found = []
    for member in members:
        for figure_id in member.figure_ids or []:
            if figure_id not in found:
                found.append(figure_id)
    return found


def _merged_named(members, attr):
    found = []
    seen = set()
    for member in members:
        for party in getattr(member, attr, None) or []:
            key = (
                party.get("role"),
                party.get("name_normalised") or normalise_name(party.get("name") or ""),
                party.get("source_url"),
            )
            if key in seen:
                continue
            seen.add(key)
            found.append(dict(party))
    return found


def _role_keys(lead, roles):
    names = set()
    tickers = set()
    for party in list(getattr(lead, "parties", None) or []) + list(getattr(lead, "counterparties", None) or []):
        if (party.get("role") or "").casefold() not in roles:
            continue
        name = party.get("name")
        if name and not is_common_heading(name):
            names.add(normalise_name(name))
        ticker = party.get("ticker")
        if ticker:
            tickers.add(str(ticker).upper())
    return names, tickers


def _identity(lead):
    names = set()
    if lead.company:
        names.add(normalise_name(lead.company))
    if lead.ticker:
        names.add(normalise_name(lead.ticker))
    tickers = {str(lead.ticker).upper()} if lead.ticker else set()
    return names, tickers


def _blob(lead):
    chunks = []
    for item in [lead, *list(getattr(lead, "members", None) or [])]:
        figure = getattr(item, "figure", None)
        chunks.extend(
            [
                getattr(item, "company", None),
                getattr(item, "ticker", None),
                getattr(item, "summary", None),
            ]
        )
        if figure is not None:
            chunks.extend([figure.value_text, figure.instrument, figure.figure_type])
        for party in list(getattr(item, "parties", None) or []) + list(getattr(item, "counterparties", None) or []):
            chunks.append(party.get("name"))
            chunks.append(party.get("ticker"))
    return " ".join(str(chunk or "") for chunk in chunks).casefold()


def _related_payload(lead):
    figure = lead.figure
    return {
        "source_scout": lead.source_scout or "",
        "lead_type": lead.lead_type,
        "ticker": lead.ticker,
        "company": lead.company or "",
        "value_text": getattr(figure, "value_text", "") or "",
        "instrument": getattr(figure, "instrument", "") or "",
        "source_url": lead.source_url or "",
    }


def _relevance_rules():
    from pipeline.prompts import RULES

    path = RULES / "macro_relevance.md"
    if not path.is_file():
        raise FileNotFoundError(f"missing macro relevance file {path}")
    rules = {}
    current = None
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            current = stripped[3:].strip().upper()
            rules[current] = []
            continue
        if current is None or not stripped or stripped.startswith("#"):
            continue
        for part in stripped.split(","):
            word = part.strip()
            if word:
                rules[current].append(word)
    return rules
