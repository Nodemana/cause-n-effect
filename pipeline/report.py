"""Markdown reports written beside the agent queue.

With the default queue `data/agent_queue`, ranking writes
`data/reports/<run_id>-candidates.md`. The Legate selection writes one file
per selected thesis at `data/reports/<run_id>/<rank>-<ticker>.md`.
"""

import re
from datetime import date, timedelta
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


def write_candidates_report(state, ranked, conn=None):
    """Write every ranked thesis, including its Praetorian flaw list."""
    ordered = sorted(ranked, key=lambda lead: lead.rank or 0)
    path = _report_path(state, "-candidates.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    prices, levels = _report_markets(conn, ordered, getattr(state, "started", None))
    path.write_text(
        render_candidates(
            state.run_id,
            ordered,
            macro=getattr(state, "macro", None),
            prices=prices,
            levels=levels,
        ),
        encoding="utf-8",
    )
    return path


def write_selected_reports(state, selected, conn=None):
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
    prices, levels = _report_markets(conn, ordered, getattr(state, "started", None))
    for lead in ordered:
        rank = getattr(lead, "pick_order", None) or lead.rank or 0
        name = f"{rank}-{_file_slug(lead)}.md"
        if name in used:
            suffix = getattr(lead, "real_id", None) or getattr(lead, "synthetic_id", None) or "thesis"
            name = f"{rank}-{_file_slug(lead)}-{suffix}.md"
        used.add(name)
        (directory / name).write_text(
            render_selected(lead, macro=macro, prices=prices, levels=levels),
            encoding="utf-8",
        )
    return directory


def selected_directory(state):
    base = getattr(state, "reports_dir", None) or reports_directory(state.queue_dir)
    return Path(base) / str(state.run_id)


def _report_path(state, suffix):
    directory = getattr(state, "reports_dir", None) or reports_directory(state.queue_dir)
    return Path(directory) / f"{state.run_id}{suffix}"


def render_candidates(run_id, ranked, *, macro, prices=None, levels=None):
    count = len(ranked)
    thesis_word = "thesis" if count == 1 else "theses"
    lines = [
        f"# Candidate theses for {run_id}",
        "",
        f"{count} candidate {thesis_word}. The Legate selects at most 5.",
        "",
    ]
    if not ranked:
        lines.append("No candidate theses.")
        lines.append("")
        return "\n".join(lines)
    for lead in ranked:
        lines.extend(_lead_section(lead, macro=macro, prices=prices, levels=levels))
    return "\n".join(lines)


def render_selected(lead, *, macro, prices=None, levels=None):
    """One selected thesis: price table, bottom line, then the sections."""
    company = (lead.company or "").strip()
    shown = _display_ticker(lead)
    title = shown
    if company and company != shown:
        title = f"{shown} — {company}"
    rank = getattr(lead, "pick_order", None) or lead.rank or 0
    lines = [
        f"# Rank {rank}. {title}",
        "",
        f"**Lead id:** {getattr(lead, 'real_id', None) or 'none'}",
        "",
    ]
    lines.extend(_thesis_body(lead, macro=macro, prices=prices, levels=levels))
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


def _lead_section(lead, *, macro, prices, levels):
    company = (lead.company or "").strip()
    shown = _display_ticker(lead)
    title = shown
    if company and company != shown:
        title = f"{shown} — {company}"
    lines = [
        f"## Rank {lead.rank}. {title}",
        "",
        f"**Lead id:** {getattr(lead, 'real_id', None) or 'none'}",
        "",
    ]
    lines.extend(_thesis_body(lead, macro=macro, prices=prices, levels=levels))
    return lines


def _thesis_body(lead, *, macro, prices, levels):
    thesis = lead.thesis
    return [
        _price_table(lead, prices or {}, levels or {}),
        "",
        "### Bottom line",
        "",
        _bottom_line(thesis),
        "",
        "### Cause → effect",
        "",
        _cause_bullets(lead),
        "",
        "### Thesis",
        "",
        _thesis_prose(thesis),
        "",
        "### Evidence",
        "",
        _evidence_block(lead),
        "",
        "### Risks",
        "",
        _risk_bullets(thesis),
        "",
        "### Assumptions",
        "",
        _assumption_bullets(thesis),
        "",
        "### Praetorian critique",
        "",
        _praetorian_block(thesis),
        "",
        "### Legate rationale",
        "",
        _text(getattr(lead, "selection_rationale", None)) or "none",
        "",
        "### Macro context",
        "",
        _macro_for(lead, macro),
        "",
    ]


def _report_markets(conn, leads, as_of):
    if conn is None:
        return {}, {}
    from pipeline.prices import quotes_for

    tickers = []
    series = []
    for lead in leads:
        tickers.extend(_stock_tickers(lead))
        tickers.extend(_named_tickers(lead))
        series.extend(_cause_series(lead))
    return quotes_for(conn, tickers, as_of), series_levels(conn, series, as_of)


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
        # A long single block reads badly on a phone. Start a new paragraph
        # at the thesis's own section labels.
        for piece in re.split(r"\s+(?=(?:Effect|Priced-in check|Conclusion):)", line):
            if piece:
                paragraphs.append(piece)
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
    lines = str(value or "").replace("\r\n", "\n").splitlines()
    # A multi-line value already has one item per line. Only a legacy
    # single-line value joined with semicolons is split on them.
    legacy = len([line for line in lines if line.strip()]) == 1
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("- "):
            line = line[2:].strip()
        elif line.startswith("-"):
            line = line[1:].strip()
        if legacy and ";" in line and not line.startswith("http"):
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
    flaws = list(review.get("flaws") or [])
    if not flaws:
        return "No flaws recorded."
    rank = {"high": 0, "moderate": 1, "low": 2}
    flaws.sort(key=lambda item: rank.get(str(item.get("severity") or "").casefold(), 9))
    lines = [
        "| Severity | Flaw | Evidence |",
        "| --- | --- | --- |",
    ]
    for item in flaws:
        lines.append(
            "| {severity} | {flaw} | {evidence} |".format(
                severity=_cell(item.get("severity") or "low"),
                flaw=_cell(item.get("flaw")),
                evidence=_cell(item.get("evidence")),
            )
        )
    return "\n".join(lines)


def _price_table(lead, prices, levels):
    rows = []
    seen = set()
    for series_id in _cause_series(lead):
        if series_id in seen:
            continue
        seen.add(series_id)
        rows.append(_series_row(series_id, levels.get(series_id)))
    for ticker in _stock_tickers(lead):
        if ticker in seen:
            continue
        seen.add(ticker)
        rows.append(_quote_row(ticker, prices.get(ticker)))
    for ticker in _named_tickers(lead):
        if ticker in seen or ticker not in prices:
            continue
        seen.add(ticker)
        rows.append(_quote_row(ticker, prices.get(ticker)))
    if not rows:
        rows.append("| n/a | n/a | n/a | n/a | n/a |")
    header = [
        "| Ticker | Price (as-of, currency) | YTD % | 1Y % | 52-wk range |",
        "| --- | --- | --- | --- | --- |",
    ]
    return "\n".join(header + rows)


def _quote_row(ticker, quote):
    if not quote:
        return f"| {_cell(ticker)} | n/a | n/a | n/a | n/a |"
    price = _level(quote.get("price"), quote.get("currency"), quote.get("as_of"))
    return "| {ticker} | {price} | {ytd} | {year} | {span} |".format(
        ticker=_cell(ticker),
        price=price,
        ytd=_pct(quote.get("price"), quote.get("prior_year_close")),
        year=_pct(quote.get("price"), quote.get("close_1y")),
        span=_span(quote.get("low_52w"), quote.get("high_52w")),
    )


def _series_row(series_id, level):
    if not level:
        return f"| {_cell(series_id)} | n/a | n/a | n/a | n/a |"
    price = _level(level.get("level"), level.get("unit"), level.get("as_of"), signed=True)
    return "| {ticker} | {price} | {ytd} | {year} | n/a |".format(
        ticker=_cell(series_id),
        price=price,
        ytd=_pct(level.get("level"), level.get("prior_year_close"), allow_negative_base=True),
        year=_pct(level.get("level"), level.get("close_1y"), allow_negative_base=True),
    )


def _bottom_line(thesis):
    risks = _bullet_items(getattr(thesis, "risks_text", None))
    biggest = risks[0] if risks else "n/a"
    conviction = _text(getattr(thesis, "conviction", None)) or "n/a"
    recommendation = _recommendation(thesis)
    if recommendation == "none":
        recommendation = "n/a"
    expected = _expected_return(thesis)
    if expected == "none":
        expected = "n/a"
    timeframe = _timeframe(thesis)
    if timeframe == "none":
        timeframe = "n/a"
    # One list item per field: consecutive quote lines without a list
    # marker run together into one paragraph in most markdown viewers.
    return "\n".join(
        [
            f"> - **Recommendation:** {recommendation}",
            f"> - **Expected return:** {expected}",
            f"> - **Timeframe:** {timeframe}",
            f"> - **Conviction:** {conviction}",
            f"> - **Biggest risk:** {biggest}",
        ]
    )


def _cause_bullets(lead):
    parts = [part.strip() for part in _cause_effect(lead).split("→")]
    kept = [part for part in parts if part]
    if not kept:
        return "- none"
    lines = []
    for index, part in enumerate(kept):
        if index == 0:
            english = _macro_cause_bullets(lead, part)
            if english:
                lines.extend(english)
                continue
        lines.append(f"- {part}")
    return "\n".join(lines)


_READING = re.compile(
    r"^(?:macro_(?:level|spread)\s+)?"
    r"(?:(?P<role>1m|3m)\s+)?"
    r"(?P<series>[A-Za-z0-9][A-Za-z0-9.-]{0,40})\s+"
    r"(?P<value>[+-]?\d+(?:\.\d+)?)\s+"
    r"on\s+(?P<day>\d{4}-\d{2}-\d{2})\s+"
    r"source\s+(?P<url>\S+?)$"
)
_CHANGE = re.compile(
    r"^(?P<key>1m_pct|3m_pct|z_score|spread|range_low|range_high)\s+"
    r"(?P<value>[+-]?\d+(?:\.\d+)?)$"
)
_WINDOW = {"1m_pct": "one month", "3m_pct": "three months"}
_REASON = {
    "1m_pct": "the one-month move",
    "3m_pct": "the three-month move",
    "z_score": "an unusual move versus the prior year",
    "spread_outside_1y_range": "the spread sitting outside its prior-year range",
}


def _macro_cause_bullets(lead, text):
    """Turn a stored macro figure into short sentences.

    The stored summary keeps the machine fields. This report does not.
    """
    if not _looks_like_macro_figure(text):
        return []
    readings = []
    changes = {}
    reason_tokens = []
    for clause in text.split(";"):
        clause = " ".join(clause.split())
        if not clause:
            continue
        reading = _READING.match(clause)
        if reading:
            readings.append(reading.groupdict())
            continue
        change = _CHANGE.match(clause)
        if change:
            changes[change.group("key")] = change.group("value")
            continue
        if clause.startswith("reasons "):
            body = re.sub(r"\s+\d{4}-\d{2}-\d{2}$", "", clause[len("reasons ") :].strip())
            reason_tokens = [token for token in body.split() if token in _REASON]
    lines = [_reading_bullet(lead, item) for item in readings]
    for key, window in _WINDOW.items():
        if key in changes:
            lines.append(_move_bullet(window, changes[key]))
    if "z_score" in changes:
        lines.append(_z_bullet(changes["z_score"]))
    spread = _spread_bullet(lead, changes, reason_tokens)
    if spread:
        lines.append(spread)
    why = _why_bullet(reason_tokens)
    if why:
        lines.append(why)
    return [f"- {line}" for line in lines if line]


def _looks_like_macro_figure(text):
    folded = text.casefold()
    return (
        folded.startswith("macro_level")
        or folded.startswith("macro_spread")
        or "1m_pct" in folded
        or "3m_pct" in folded
        or "z_score" in folded
        or "range_low" in folded
    )


def _reading_bullet(lead, item):
    name = _series_label(lead, item["series"])
    amount = f"{float(item['value']):,.2f}"
    when = item["day"]
    role = item.get("role")
    if role == "1m":
        sentence = f"One month earlier, {name} was {amount} on {when}"
    elif role == "3m":
        sentence = f"Three months earlier, {name} was {amount} on {when}"
    else:
        sentence = f"{name} was {amount} on {when}"
    url = item.get("url") or ""
    if _official_url(url):
        sentence += f" ([source]({url}))"
    return sentence + "."


def _series_label(lead, series_id):
    key = series_id.upper()
    ticker = _text(getattr(lead, "ticker", "")).upper()
    company = _text(getattr(lead, "company", ""))
    if key == ticker and company and company.upper() != key:
        return company
    return {"BRENT": "Brent", "WTI": "WTI", "BRENT-WTI": "Brent minus WTI"}.get(key, series_id)


def _move_bullet(window, value):
    number = float(value)
    if number > 0:
        return f"Up {number:.1f}% over {window}."
    if number < 0:
        return f"Down {abs(number):.1f}% over {window}."
    return f"Unchanged over {window}."


def _z_bullet(value):
    number = float(value)
    side = "above" if number >= 0 else "below"
    return f"{abs(number):.1f} standard deviations {side} its prior-year average."


def _spread_bullet(lead, changes, reason_tokens):
    if "spread" not in changes:
        return ""
    amount = f"{float(changes['spread']):,.2f}"
    name = _text(getattr(lead, "company", "")) or "The spread"
    if "range_low" in changes and "range_high" in changes:
        low = f"{float(changes['range_low']):,.2f}"
        high = f"{float(changes['range_high']):,.2f}"
        if "spread_outside_1y_range" in reason_tokens:
            return f"{name} was {amount}, outside its prior-year range of {low} to {high}."
        return f"{name} was {amount}. Its prior-year range was {low} to {high}."
    return f"{name} was {amount}."


def _why_bullet(reason_tokens):
    labels = []
    for token in reason_tokens:
        if token == "spread_outside_1y_range":
            continue
        label = _REASON.get(token)
        if label and label not in labels:
            labels.append(label)
    if not labels:
        return ""
    if len(labels) == 1:
        joined = labels[0]
    elif len(labels) == 2:
        joined = f"{labels[0]} and {labels[1]}"
    else:
        joined = ", ".join(labels[:-1]) + ", and " + labels[-1]
    return f"This lead is here because {joined} cleared its bar."


def _stock_tickers(lead):
    series = set(_cause_series(lead))
    return [ticker for ticker in _tickers(lead) if ticker not in series]


def _named_tickers(lead):
    """Tickers a macro thesis names as winners or losers in its recommendation.

    These are candidates only: the price table shows one when an official
    quote for it is stored, so an ordinary capitalised word never becomes a row.
    """
    if getattr(lead, "lead_type", "") != "macro" or lead.thesis is None:
        return []
    text = _text(getattr(lead.thesis, "recommendation", None))
    found = []
    for token in re.findall(r"\b[A-Z]{1,5}\b", text):
        if token not in found and token not in set(_cause_series(lead)):
            found.append(token)
    return found


def _cause_series(lead):
    if getattr(lead, "lead_type", "") != "macro":
        return []
    ids = []
    for part in re.split(r"[,;/]", _display_ticker(lead)):
        text = part.strip().upper()
        if _TICKER.fullmatch(text) and text not in ids:
            ids.append(text)
    expanded = []
    for series_id in ids:
        expanded.append(series_id)
        if series_id in ("BRENT-WTI", "BRENT", "WTI"):
            for leg in ("BRENT", "WTI"):
                if leg not in expanded:
                    expanded.append(leg)
    return expanded


def series_levels(conn, series_ids, as_of):
    """Latest macro level, prior-year close, and the close 52 weeks earlier.

    Each figure is a stored observation. A missing observation stays absent.
    """
    wanted = []
    for series_id in series_ids:
        key = str(series_id or "").strip().upper()
        if key and key not in wanted:
            wanted.append(key)
    if not wanted or conn is None or not _macro_tables(conn):
        return {}
    ceiling = str(as_of or "")[:10] or "9999-12-31"
    found = {}
    for series_id in wanted:
        latest = conn.execute(
            """
            SELECT s.name AS name, s.unit AS unit, o.obs_date AS obs_date, o.value AS value
            FROM macro_observations AS o
            JOIN macro_series AS s ON s.series_id = o.series_id
            WHERE s.series_id = ? AND o.obs_date <= ?
            ORDER BY o.obs_date DESC
            LIMIT 1
            """,
            (series_id, ceiling),
        ).fetchone()
        if latest is None:
            continue
        anchor = latest["obs_date"]
        prior = _obs_on_or_before(conn, series_id, _prior_year_end(anchor))
        year_ago = _obs_on_or_before(conn, series_id, _weeks_before(anchor, 52))
        found[series_id] = {
            "name": latest["name"],
            "unit": latest["unit"],
            "level": latest["value"],
            "as_of": anchor,
            "prior_year_close": None if prior is None else prior["value"],
            "close_1y": None if year_ago is None else year_ago["value"],
        }
    return found


def _macro_tables(conn):
    rows = conn.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type = 'table' AND name IN ('macro_series', 'macro_observations')
        """
    ).fetchall()
    found = {row["name"] for row in rows}
    return "macro_series" in found and "macro_observations" in found


def _obs_on_or_before(conn, series_id, day):
    if not day:
        return None
    return conn.execute(
        """
        SELECT obs_date, value
        FROM macro_observations
        WHERE series_id = ? AND obs_date <= ?
        ORDER BY obs_date DESC
        LIMIT 1
        """,
        (series_id, day),
    ).fetchone()


def _prior_year_end(obs_date):
    try:
        anchor = date.fromisoformat(str(obs_date)[:10])
    except ValueError:
        return ""
    return date(anchor.year - 1, 12, 31).isoformat()


def _weeks_before(obs_date, weeks):
    try:
        anchor = date.fromisoformat(str(obs_date)[:10])
    except ValueError:
        return ""
    return (anchor - timedelta(days=7 * int(weeks))).isoformat()


def _level(value, currency, as_of, signed=False):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "n/a"
    if not signed and number <= 0:
        return "n/a"
    amount = f"{number:,.2f}"
    unit = _text(currency)
    when = _text(as_of)[:10]
    label = amount if not unit else f"{amount} {unit}"
    if not when:
        return label
    return f"{label} ({when})"


def _pct(latest, base, allow_negative_base=False):
    try:
        latest_value = float(latest)
        base_value = float(base)
    except (TypeError, ValueError):
        return "n/a"
    if allow_negative_base:
        if base_value == 0:
            return "n/a"
        change = (latest_value - base_value) / abs(base_value) * 100
    else:
        if base_value <= 0 or latest_value <= 0:
            return "n/a"
        change = (latest_value - base_value) / base_value * 100
    return f"{change:+.1f}%"


def _span(low, high):
    try:
        low_value = float(low)
        high_value = float(high)
    except (TypeError, ValueError):
        return "n/a"
    if low_value <= 0 or high_value <= 0 or low_value > high_value:
        return "n/a"
    return f"{low_value:,.2f}–{high_value:,.2f}"


def _cell(value):
    return _text(value).replace("|", "/") or "n/a"


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
