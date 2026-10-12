"""Markdown reports written beside the agent queue.

With the default queue `data/agent_queue`, ranking writes
`data/reports/<run_id>-candidates.md` and the Legate selection writes
`data/reports/<run_id>-final.md`.
"""

from pathlib import Path


def reports_directory(queue_dir):
    return str(Path(queue_dir).resolve().parent / "reports")


def write_candidates_report(state, ranked):
    """Write every ranked thesis, including its Praetorian flaw list."""
    ordered = sorted(ranked, key=lambda lead: lead.rank or 0)
    path = _report_path(state, "-candidates.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_candidates(state.run_id, ordered, macro=getattr(state, "macro", None)),
        encoding="utf-8",
    )
    return path


def write_final_report(state, selected):
    """Write the theses the Legate selected, in pick order."""
    ordered = sorted(selected, key=lambda lead: getattr(lead, "pick_order", None) or 0)
    path = _report_path(state, "-final.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_final(state.run_id, ordered, macro=getattr(state, "macro", None)),
        encoding="utf-8",
    )
    return path


def _report_path(state, suffix):
    directory = getattr(state, "reports_dir", None) or reports_directory(state.queue_dir)
    return Path(directory) / f"{state.run_id}{suffix}"


def render_candidates(run_id, ranked, *, macro):
    count = len(ranked)
    thesis_word = "thesis" if count == 1 else "theses"
    lines = [
        f"# Candidate theses for {run_id}",
        "",
        f"Sweep macro context: {_text(macro) or 'none'}",
        "",
        f"{count} candidate {thesis_word}. The Legate selects at most 5.",
        "",
    ]
    if not ranked:
        lines.append("No candidate theses.")
        lines.append("")
        return "\n".join(lines)
    for lead in ranked:
        lines.extend(_lead_section(lead, selection=False))
    return "\n".join(lines)


def render_final(run_id, selected, *, macro):
    count = len(selected)
    thesis_word = "thesis" if count == 1 else "theses"
    lines = [
        f"# Selected theses for {run_id}",
        "",
        f"Sweep macro context: {_text(macro) or 'none'}",
        "",
        f"{count} selected {thesis_word}.",
        "",
    ]
    if not selected:
        lines.append("No theses selected.")
        lines.append("")
        return "\n".join(lines)
    for lead in selected:
        lines.extend(_lead_section(lead, selection=True))
    return "\n".join(lines)


def _lead_section(lead, *, selection):
    thesis = lead.thesis
    company = (lead.company or "").strip()
    title = f"{lead.ticker}"
    if company and company != lead.ticker:
        title = f"{lead.ticker} — {company}"
    tickers = ", ".join(_tickers(lead)) or "none"
    lines = [
        f"## Rank {lead.rank}. {title}",
        "",
        f"**Lead id:** {getattr(lead, 'real_id', None) or 'none'}",
        f"**Lead:** {lead.lead_type}",
        f"**Scout:** {lead.source_scout}",
        f"**Tickers:** {tickers}",
    ]
    if selection:
        lines.append(f"**Legate rationale:** {_text(getattr(lead, 'selection_rationale', None)) or 'none'}")
    lines.extend(
        [
            "",
        ]
    )
    return lines + [
        "### Thesis",
        "",
        _thesis_paragraph(thesis),
        "",
        "### Cause → effect",
        "",
        _cause_effect(lead),
        "",
        "### Evidence",
        "",
        _evidence_block(lead),
        "",
        "### Praetorian",
        "",
        _praetorian_block(thesis),
        "",
        "### Macro context",
        "",
        _text(getattr(thesis, "macro_context", None)) or "none",
        "",
    ]


def _thesis_paragraph(thesis):
    text = _text(getattr(thesis, "markdown", None))
    marker = "## Thesis"
    if marker not in text:
        return text or "none"
    body = text.split(marker, 1)[1].lstrip("\n")
    kept = []
    for line in body.splitlines():
        if line.startswith("## ") or line.startswith("Source:"):
            break
        kept.append(line)
    paragraph = "\n".join(kept).strip()
    return paragraph or "none"


def _cause_effect(lead):
    members = list(getattr(lead, "members", None) or [])
    if members:
        return " → ".join(_step(member) for member in members)
    if lead.lead_type == "macro":
        cause = _text(lead.summary) or f"Macro {lead.ticker}"
        effects = []
        for item in getattr(lead, "related_findings", None) or []:
            name = _text(item.get("company")) or _text(item.get("ticker"))
            ticker = _text(item.get("ticker"))
            kind = _text(item.get("lead_type"))
            url = _text(item.get("source_url"))
            piece = " ".join(part for part in (name, ticker, kind) if part)
            if url:
                piece = f"{piece} ({url})"
            if piece:
                effects.append(piece)
        if effects:
            return cause + " → " + " → ".join(effects)
        return cause
    start = _step(lead)
    effects = []
    thesis = lead.thesis
    if thesis is not None:
        for role, label in (("suppliers", "supplier"), ("customers", "customer"), ("buyers", "buyer")):
            for item in getattr(thesis, role) or []:
                name = _text(item.get("name"))
                ticker = _text(item.get("ticker"))
                piece = " ".join(part for part in (label, name, ticker) if part)
                if piece:
                    effects.append(piece)
        recommendation = _text(getattr(thesis, "recommendation", None))
        if recommendation and recommendation.upper() != (lead.ticker or "").upper():
            effects.append(f"recommendation {recommendation}")
    if effects:
        return start + " → " + " → ".join(effects)
    return start


def _step(lead):
    label = _step_label(lead)
    company = _text(lead.company)
    if company == _text(lead.ticker):
        company = ""
    piece = " ".join(part for part in (label, lead.ticker, company) if part)
    url = _text(lead.source_url)
    if url:
        piece = f"{piece} ({url})"
    return piece


def _step_label(lead):
    if lead.lead_type == "insider_buying":
        return "Form 4"
    if lead.lead_type == "accumulation":
        figure = getattr(lead, "figure", None)
        blob = " ".join(
            _text(bit)
            for bit in (
                lead.source_url,
                getattr(figure, "instrument", None),
                getattr(figure, "value_text", None),
                getattr(figure, "source_type", None),
            )
        ).casefold()
        if "604" in blob or "asx.com" in blob or "asx announcement" in blob:
            return "ASX 604"
        return "13D/13G"
    return {
        "capex": "Spender",
        "orders_backlog": "Supplier (backlog)",
        "debt_raise": "Funding",
        "macro": "Macro",
    }.get(lead.lead_type, lead.lead_type or "Lead")


def _tickers(lead):
    found = []

    def add(value):
        text = _text(value).upper()
        if text and text not in found:
            found.append(text)

    add(lead.ticker)
    for member in getattr(lead, "members", None) or []:
        add(member.ticker)
    thesis = lead.thesis
    if thesis is not None:
        add(getattr(thesis, "recommendation", None))
        for role in ("suppliers", "customers", "buyers"):
            for item in getattr(thesis, role) or []:
                add(item.get("ticker"))
    for item in getattr(lead, "related_findings", None) or []:
        add(item.get("ticker"))
    return found


def _evidence_block(lead):
    rows = _evidence_links(lead)
    if not rows:
        return "- none"
    return "\n".join(f"- {label}: {url}" for url, label in rows)


def _evidence_links(lead):
    found = []
    seen = set()

    def add(url, label):
        url = _text(url)
        if not url.startswith("http") or url in seen:
            return
        seen.add(url)
        found.append((url, _text(label) or "source"))

    add(lead.source_url, lead.ticker or "source")
    figure = getattr(lead, "figure", None)
    if figure is not None:
        add(getattr(figure, "source_url", None), f"{lead.ticker} {getattr(figure, 'figure_type', '')}".strip())
    for member in getattr(lead, "members", None) or []:
        add(member.source_url, f"{member.ticker} {member.lead_type}".strip())
        member_figure = getattr(member, "figure", None)
        if member_figure is not None:
            add(
                getattr(member_figure, "source_url", None),
                f"{member.ticker} {getattr(member_figure, 'figure_type', '')}".strip(),
            )
    for item in getattr(lead, "related_findings", None) or []:
        add(item.get("source_url"), f"{item.get('ticker') or ''} {item.get('lead_type') or ''}".strip())
    for party in list(getattr(lead, "counterparties", None) or []):
        add(party.get("source_url"), party.get("name") or "counterparty")
    return found


def _praetorian_block(thesis):
    review = getattr(thesis, "review", None) or {}
    flaws = review.get("flaws") or []
    if not flaws:
        return "No flaws recorded."
    lines = []
    for item in flaws:
        lines.append(f"- **{item.get('severity') or 'low'}** — {_text(item.get('flaw'))}")
        lines.append(f"  Evidence: {_text(item.get('evidence'))}")
    return "\n".join(lines)


def _text(value):
    return " ".join(str(value or "").split())
