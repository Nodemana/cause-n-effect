"""Markdown reports written beside the agent queue.

With the default queue `data/agent_queue`, ranking writes
`data/reports/<run_id>-candidates.md`. The Legate selection writes one file
per selected thesis at `data/reports/<run_id>/<rank>-<ticker>.md`.
"""

import re
from pathlib import Path
from urllib.parse import urlsplit

_TICKER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,40}$")
_OFFICIAL_HOSTS = (
    "sec.gov",
    "asx.com.au",
    "asx.api.markitdigital.com",
    "eia.gov",
    "stlouisfed.org",
    "bls.gov",
    "abs.gov.au",
    "rba.gov.au",
    "newyorkfed.org",
)


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


def write_selected_reports(state, selected):
    """Write one markdown file for each thesis the Legate selected."""
    directory = selected_directory(state)
    if directory.exists():
        for old in directory.glob("*.md"):
            old.unlink()
    ordered = sorted(selected, key=lambda lead: getattr(lead, "pick_order", None) or lead.rank or 0)
    if not ordered:
        if directory.exists() and not any(directory.iterdir()):
            directory.rmdir()
        return None
    directory.mkdir(parents=True, exist_ok=True)
    used = set()
    macro = getattr(state, "macro", None)
    for lead in ordered:
        rank = getattr(lead, "pick_order", None) or lead.rank or 0
        name = f"{rank}-{_file_slug(lead)}.md"
        if name in used:
            suffix = getattr(lead, "real_id", None) or getattr(lead, "synthetic_id", None) or "thesis"
            name = f"{rank}-{_file_slug(lead)}-{suffix}.md"
        used.add(name)
        (directory / name).write_text(render_selected(lead, macro=macro), encoding="utf-8")
    return directory


def selected_directory(state):
    base = getattr(state, "reports_dir", None) or reports_directory(state.queue_dir)
    return Path(base) / str(state.run_id)


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


def render_selected(lead, *, macro):
    """One selected thesis, then the full Praetorian critique."""
    thesis = lead.thesis
    company = (lead.company or "").strip()
    shown = _display_ticker(lead)
    title = shown
    if company and company != shown:
        title = f"{shown} — {company}"
    rank = getattr(lead, "pick_order", None) or lead.rank or 0
    price_run = _text(getattr(thesis, "price_run", None)) or "none"
    lines = [
        f"# Rank {rank}. {title}",
        "",
        f"**Lead id:** {getattr(lead, 'real_id', None) or 'none'}",
        f"**Lead:** {lead.lead_type}",
        f"**Scout:** {lead.source_scout}",
        f"**Tickers:** {', '.join(_tickers(lead)) or 'none'}",
        f"**Price run:** {price_run}",
        f"**Legate rationale:** {_text(getattr(lead, 'selection_rationale', None)) or 'none'}",
        "",
    ]
    lines.extend(_judgement_sections(thesis))
    lines.extend([
        "### Cause → effect",
        "",
        _cause_effect(lead),
        "",
        "### Evidence",
        "",
        _evidence_block(lead),
        "",
        "### Macro context",
        "",
        _macro_for(lead, macro),
        "",
        "### Price run",
        "",
        price_run,
        "",
        "### Praetorian critique",
        "",
        _praetorian_block(thesis),
        "",
    ])
    return "\n".join(lines)


def _file_slug(lead):
    shown = _display_ticker(lead)
    if _TICKER.fullmatch(shown):
        return shown
    raw = _text(lead.company) or _text(lead.lead_type) or "thesis"
    slug = re.sub(r"[^A-Za-z0-9]+", "-", raw).strip("-").lower()
    return (slug[:40] or "thesis")


def _display_ticker(lead):
    parts = []
    for part in re.split(r"[,;/]", _text(lead.ticker)):
        text = part.strip().upper()
        if _TICKER.fullmatch(text) and text not in parts:
            parts.append(text)
    if parts:
        return ", ".join(parts)
    return _text(lead.ticker)


def _lead_section(lead, *, selection):
    thesis = lead.thesis
    company = (lead.company or "").strip()
    shown = _display_ticker(lead)
    title = shown
    if company and company != shown:
        title = f"{shown} — {company}"
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
    lines.extend(_judgement_sections(thesis))
    return lines + [
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
        _macro_for(lead, None),
        "",
    ]


def _judgement_sections(thesis):
    return [
        "### Thesis",
        "",
        _thesis_prose(thesis),
        "",
        "### Recommendation",
        "",
        _recommendation(thesis),
        "",
        "### Expected return",
        "",
        _expected_return(thesis),
        "",
        "### Risks",
        "",
        _risk_bullets(thesis),
        "",
        "### Timeframe",
        "",
        _timeframe(thesis),
        "",
        "### Assumptions",
        "",
        _assumption_bullets(thesis),
        "",
    ]


def _thesis_prose(thesis):
    raw = str(getattr(thesis, "markdown", None) or "").replace("\r\n", "\n")
    if "## Thesis" in raw:
        body = raw.split("## Thesis", 1)[1]
    else:
        body = raw
    body = re.split(r"\n(?:##\s+\S|Source:)", body, maxsplit=1)[0]
    flattened = " ".join(body.split())
    if "## Recommendation" in flattened or "Expected return:" in flattened:
        body = re.split(
            r"##\s+Recommendation|Expected return:|Risks:|Timeframe:|##\s+Assumptions",
            body,
            maxsplit=1,
        )[0]
    paragraphs = []
    for block in re.split(r"\n\s*\n", body.strip()):
        line = " ".join(block.split())
        if line:
            paragraphs.append(line)
    return "\n\n".join(paragraphs) or "none"


def _recommendation(thesis):
    text = _text(getattr(thesis, "recommendation", None))
    if text:
        return text
    return _markdown_field(thesis, "Recommendation:") or "none"


def _expected_return(thesis):
    text = _text(getattr(thesis, "roi", None))
    if text:
        return text
    return _markdown_field(thesis, "Expected return:") or "none"


def _risk_bullets(thesis):
    items = _bullet_items(getattr(thesis, "risks_text", None))
    if not items:
        items = _bullet_items(_markdown_field(thesis, "Risks:"))
    if not items:
        return "- none"
    return "\n".join(f"- {item}" for item in items)


def _assumption_bullets(thesis):
    items = _bullet_items(getattr(thesis, "assumptions_text", None))
    if not items:
        items = _bullet_items(_markdown_after(thesis, "## Assumptions"))
    if not items:
        return "- none"
    return "\n".join(f"- {item}" for item in items)


def _timeframe(thesis):
    days = getattr(thesis, "timeframe_days", None)
    if days is None or days == "":
        raw = _markdown_field(thesis, "Timeframe:")
        return raw or "none"
    return f"{int(days)} days"


def _bullet_items(value):
    items = []
    for line in str(value or "").replace("\r\n", "\n").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("- "):
            line = line[2:].strip()
        elif line.startswith("-"):
            line = line[1:].strip()
        if ";" in line and not line.startswith("http"):
            for part in line.split(";"):
                part = part.strip()
                if part:
                    items.append(part)
            continue
        if line:
            items.append(line)
    return items


def _markdown_field(thesis, label):
    raw = " ".join(str(getattr(thesis, "markdown", None) or "").split())
    match = re.search(
        rf"{re.escape(label)}\s*(.+?)(?=\s+(?:##\s+\S|Source:|Recommendation:|Expected return:|Risks:|Timeframe:)|$)",
        raw,
    )
    if not match:
        return ""
    return match.group(1).strip()


def _markdown_after(thesis, heading):
    raw = str(getattr(thesis, "markdown", None) or "").replace("\r\n", "\n")
    if heading not in raw:
        return ""
    body = raw.split(heading, 1)[1]
    kept = []
    for line in body.splitlines():
        if line.startswith("## "):
            break
        kept.append(line)
    return "\n".join(kept)


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
        shown = _display_ticker(lead)
        if recommendation and recommendation.upper() != shown.upper() and recommendation.upper() != (lead.ticker or "").upper():
            effects.append(f"recommendation {recommendation}")
    if effects:
        return start + " → " + " → ".join(effects)
    return start


def _step(lead):
    label = _step_label(lead)
    company = _text(lead.company)
    ticker = _display_ticker(lead)
    if company == ticker or company == _text(lead.ticker):
        company = ""
    piece = " ".join(part for part in (label, ticker, company) if part)
    url = _text(lead.source_url)
    if _official_url(url):
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
        for part in re.split(r"[,;/]", _text(value)):
            text = part.strip().upper()
            if _TICKER.fullmatch(text) and text not in found:
                found.append(text)

    add(lead.ticker)
    for member in getattr(lead, "members", None) or []:
        add(member.ticker)
    thesis = lead.thesis
    if thesis is not None:
        for role in ("suppliers", "customers", "buyers"):
            for item in getattr(thesis, role) or []:
                add(item.get("ticker"))
    for item in getattr(lead, "related_findings", None) or []:
        add(item.get("ticker"))
    return found


def _macro_for(lead, fallback):
    thesis = getattr(lead, "thesis", None)
    raw = str(getattr(thesis, "macro_context", None) or "").strip()
    if not raw:
        raw = str(fallback or "").strip()
    if not raw:
        return "none"
    if not _is_catalog_dump(raw):
        return _paragraphs(raw) or "none"
    segments = _macro_segments(raw)
    relevant = _relevant_series(lead, segments)
    kept = []
    for segment in segments:
        series_id = segment.split()[0].upper() if segment.split() else ""
        if series_id in relevant:
            kept.append(segment)
    if not kept:
        return "none"
    return "\n".join(f"- {item}" for item in kept)


def _is_catalog_dump(text):
    folded = text.lstrip().casefold()
    return folded.startswith("macro scout cache:") or "unavailable:" in folded


def _macro_segments(text):
    body = text.strip()
    if body.casefold().startswith("macro scout cache:"):
        body = body.split(":", 1)[1].strip()
    cut = body.casefold().find("unavailable:")
    if cut != -1:
        body = body[:cut].rstrip(" .;")
    return [part.strip() for part in body.split(";") if part.strip()]


def _relevant_series(lead, segments):
    from pipeline.scout_groups import _relevance_rules

    rules = _relevance_rules()
    blob = _lead_blob(lead)
    ticker = _text(getattr(lead, "ticker", "")).upper()
    ticker_ids = {part.strip() for part in re.split(r"[,;/]", ticker) if part.strip()}
    matched = set()
    for segment in segments:
        series_id = segment.split()[0].upper() if segment.split() else ""
        if not series_id:
            continue
        keywords = rules.get(series_id)
        if series_id in ticker_ids:
            matched.add(series_id)
            continue
        if keywords is None:
            if series_id.casefold() in blob:
                matched.add(series_id)
            continue
        if any(word.casefold() in blob for word in keywords if word):
            matched.add(series_id)
    return matched


def _lead_blob(lead):
    thesis = getattr(lead, "thesis", None)
    chunks = [
        getattr(lead, "ticker", ""),
        getattr(lead, "company", ""),
        getattr(lead, "summary", ""),
        getattr(lead, "lead_type", ""),
        _thesis_prose(thesis) if thesis is not None else "",
        getattr(thesis, "recommendation", "") if thesis is not None else "",
    ]
    for member in getattr(lead, "members", None) or []:
        chunks.append(getattr(member, "ticker", ""))
        chunks.append(getattr(member, "company", ""))
        chunks.append(getattr(member, "summary", ""))
    if thesis is not None:
        for role in ("suppliers", "customers", "buyers"):
            for item in getattr(thesis, role) or []:
                if isinstance(item, dict):
                    chunks.append(item.get("name"))
                    chunks.append(item.get("ticker"))
    for item in getattr(lead, "related_findings", None) or []:
        chunks.append(item.get("company"))
        chunks.append(item.get("ticker"))
        chunks.append(item.get("lead_type"))
    return " ".join(str(chunk or "") for chunk in chunks).casefold()


def _paragraphs(value):
    text = str(value or "").replace("\r\n", "\n").strip()
    blocks = []
    for block in re.split(r"\n\s*\n", text):
        line = " ".join(block.split())
        if line:
            blocks.append(line)
    return "\n\n".join(blocks)


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
        if not _official_url(url) or url in seen:
            return
        seen.add(url)
        found.append((url, _text(label) or "source"))

    add(lead.source_url, _display_ticker(lead) or "source")
    figure = getattr(lead, "figure", None)
    if figure is not None:
        add(getattr(figure, "source_url", None), f"{_display_ticker(lead)} {getattr(figure, 'figure_type', '')}".strip())
    for member in getattr(lead, "members", None) or []:
        add(member.source_url, f"{_display_ticker(member)} {member.lead_type}".strip())
        member_figure = getattr(member, "figure", None)
        if member_figure is not None:
            add(
                getattr(member_figure, "source_url", None),
                f"{_display_ticker(member)} {getattr(member_figure, 'figure_type', '')}".strip(),
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


def _official_url(url):
    text = _text(url)
    if not text.startswith("http"):
        return False
    host = (urlsplit(text).hostname or "").lower().rstrip(".")
    for suffix in _OFFICIAL_HOSTS:
        if host == suffix or host.endswith("." + suffix):
            return True
    return False


def _text(value):
    return " ".join(str(value or "").split())
