"""Deterministic gates: thresholds, then dedupe. No model calls."""

import re
from datetime import date

from pipeline.load import as_of, classify, exchange_for, extract_cik, filing_date, period_end
from pipeline.marketcap import criterion_threshold, resolve_market_cap
from pipeline.names import normalise_name

# Rubric and scout defaults the figures can support without a market-cap feed.
# Debt uses the scout's minimum deal size. Insider dollars, stake percent,
# capex year-over-year, backlog quarter-over-quarter, and the accumulation
# window match rubric version 1.
INSIDER_MIN_DOLLARS = 10_000
STAKE_MIN_PCT = 5
DEBT_MIN_DOLLARS = 50_000_000
CAPEX_GROWTH_PCT = 10
BACKLOG_GROWTH_PCT = 10
ACCUMULATION_WINDOW_DAYS = 90
STALE_PERIOD_DAYS = 120
SIMILAR_VALUE_BAND = 0.20
_SPAN_RANK = {"quarter": 0, "ttm": 1, "ytd": 2, "annual": 3, "instant": 4}
_METRIC_LABELS = {"orders", "order", "backlog", "revenue"}

_STOP_PARTIES = {
    "quarter",
    "ytd",
    "annual",
    "ttm",
    "instant",
    "ceo",
    "bond",
    "facility",
    "loan",
    "filing",
}
_IN_FLIGHT = ("fresh", "assigned", "researched", "presented", "acted", "position")


class Rating:
    def __init__(
        self,
        magnitude=None,
        magnitude_rationale=None,
        novelty=None,
        novelty_rationale=None,
        attention=None,
        attention_rationale=None,
        timing=None,
        timing_rationale=None,
    ):
        self.magnitude = magnitude
        self.magnitude_rationale = magnitude_rationale
        self.novelty = novelty
        self.novelty_rationale = novelty_rationale
        self.attention = attention
        self.attention_rationale = attention_rationale
        self.timing = timing
        self.timing_rationale = timing_rationale

    @classmethod
    def from_model(cls, data):
        return cls(
            magnitude=_label(data.get("magnitude")),
            magnitude_rationale=_line(data.get("magnitude_rationale")),
            novelty=_label(data.get("novelty")),
            novelty_rationale=_line(data.get("novelty_rationale")),
            attention=_label(data.get("attention")),
            attention_rationale=_line(data.get("attention_rationale")),
            timing=_label(data.get("timing")),
            timing_rationale=_line(data.get("timing_rationale")),
        )

    def complete(self):
        return all(
            (
                self.magnitude,
                self.magnitude_rationale,
                self.novelty,
                self.novelty_rationale,
                self.attention,
                self.attention_rationale,
                self.timing,
                self.timing_rationale,
            )
        )


class PlannedReject:
    def __init__(self, figure, lead_type, stage, reason, rating=None, parties=None):
        self.figure = figure
        self.lead_type = lead_type
        self.stage = stage
        self.reason = reason
        self.rating = rating
        self.parties = parties or []


class Candidate:
    def __init__(self, figure, lead_type, parties):
        self.figure = figure
        self.lead_type = lead_type
        self.parties = parties


def prefilter(conn, figures, limit=None):
    """Return (candidates, rejects) from the latest figures.

    `limit` keeps the first N lead figures in newest-announced order before
    the gates run. Balance-sheet tags that are not a lead type are skipped
    and are not rejects, and they do not consume `limit`. Older periods and
    extra capex spans are not rated.
    """
    ordered = [figure for figure in figures if classify(figure.scout, figure.figure_type) is not None]
    if limit is not None:
        ordered = ordered[: int(limit)]
    by_name, cik_by_ticker = party_index(figures)
    candidates = []
    rejects = []
    classified = []
    for figure in ordered:
        lead_type = classify(figure.scout, figure.figure_type)
        if lead_type is None:
            continue
        if int(figure.verified) != 1:
            parties = extract_parties(figure, lead_type, by_name, cik_by_ticker)
            rejects.append(
                PlannedReject(figure, lead_type, "deterministic", "unverified figure", parties=parties)
            )
            continue
        classified.append((figure, lead_type))
    classified, span_dropped = _prefer_spans(classified)
    classified, older = _latest_periods(classified)
    for figure, lead_type in span_dropped:
        parties = extract_parties(figure, lead_type, by_name, cik_by_ticker)
        rejects.append(
            PlannedReject(
                figure,
                lead_type,
                "deterministic",
                "extra span; kept the preferred span for this figure",
                parties=parties,
            )
        )
    for figure, lead_type in older:
        parties = extract_parties(figure, lead_type, by_name, cik_by_ticker)
        rejects.append(
            PlannedReject(
                figure,
                lead_type,
                "deterministic",
                "older period kept for growth comparison",
                parties=parties,
            )
        )
    for figure, lead_type in classified:
        parties = extract_parties(figure, lead_type, by_name, cik_by_ticker)
        reason, rating = _threshold(conn, figure, lead_type)
        if reason:
            rejects.append(PlannedReject(figure, lead_type, "deterministic", reason, rating, parties))
            continue
        candidates.append(Candidate(figure, lead_type, parties))
    candidates, duplicates = _dedupe_batch(candidates)
    rejects.extend(duplicates)
    kept = []
    for candidate in candidates:
        if _in_flight(conn, candidate):
            rejects.append(
                PlannedReject(
                    candidate.figure,
                    candidate.lead_type,
                    "deterministic",
                    "duplicate of a lead already in flight",
                    parties=candidate.parties,
                )
            )
            continue
        kept.append(candidate)
    return kept, rejects


def interesting(rating):
    """A lead is emitted only when every label clears the gate."""
    if rating is None or not rating.complete():
        return False
    return (
        rating.magnitude in {"large", "moderate"}
        and rating.novelty == "first"
        and rating.attention == "unnoticed"
        and rating.timing == "early"
    )


def failure_reason(rating):
    if rating is None or not rating.complete():
        return "significance response was incomplete"
    checks = (
        ("magnitude", rating.magnitude, {"large", "moderate"}, rating.magnitude_rationale),
        ("novelty", rating.novelty, {"first"}, rating.novelty_rationale),
        ("attention", rating.attention, {"unnoticed"}, rating.attention_rationale),
        ("timing", rating.timing, {"early"}, rating.timing_rationale),
    )
    for name, label, ok, rationale in checks:
        if label not in ok:
            return f"{name} {label}: {rationale}"
    return "significance ratings below the gate"


def extract_parties(figure, lead_type, by_name, cik_by_ticker):
    found = []

    def add(role, name):
        cleaned = (name or "").strip(" .;")
        if not _is_party(cleaned):
            return
        key = normalise_name(cleaned)
        if not key:
            return
        ticker = by_name.get(key)
        cik = cik_by_ticker.get(ticker) if ticker else None
        found.append(
            {"role": role, "name": cleaned, "name_normalised": key, "ticker": ticker, "cik": cik}
        )

    text = figure.value_text or ""
    for role in ("supplier", "customer", "buyer"):
        for match in re.finditer(rf"(?i)\b{role}s?\s*:\s*([^;\n]+)", text):
            add(role, match.group(1))
    if lead_type == "accumulation":
        parts = (figure.period or "").split("|")
        if len(parts) >= 3:
            add("buyer", parts[-1])
    unique = []
    seen = set()
    for party in found:
        key = (party["role"], party["name_normalised"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(party)
    return unique


def party_index(figures):
    by_name = {}
    cik_by_ticker = {}
    for figure in figures:
        if figure.company and figure.ticker:
            by_name[normalise_name(figure.company)] = figure.ticker
        if figure.ticker:
            by_name[normalise_name(figure.ticker)] = figure.ticker
        cik = extract_cik(figure.source_url)
        if cik and figure.ticker and figure.ticker not in cik_by_ticker:
            cik_by_ticker[figure.ticker] = cik
    return by_name, cik_by_ticker


def _threshold(conn, figure, lead_type):
    if figure.value is None or figure.value <= 0:
        return "missing value", _magnitude("missing value")
    if lead_type == "insider_buying" and figure.value < INSIDER_MIN_DOLLARS:
        reason = f"below insider_buy_dollars threshold {INSIDER_MIN_DOLLARS}"
        return reason, _magnitude(reason)
    if lead_type == "debt_raise":
        currency = (figure.currency or "USD").upper()
        if currency == "USD" and figure.value < DEBT_MIN_DOLLARS:
            reason = f"below debt minimum {DEBT_MIN_DOLLARS} USD"
            return reason, _magnitude(reason)
    if lead_type == "accumulation":
        stake = _stake_pct(figure)
        if stake is None:
            reason = "stake percent missing"
            return reason, _magnitude(reason)
        if stake < STAKE_MIN_PCT:
            reason = f"below min_stake_pct threshold {STAKE_MIN_PCT:g}"
            return reason, _magnitude(reason)
        window = _outside_window(figure)
        if window:
            return window, Rating(timing="outside window", timing_rationale=window)
    cap, cap_note = resolve_market_cap(conn, figure.ticker)
    figure.market_cap = cap
    threshold = criterion_threshold(conn, lead_type, "min_market_cap")
    if threshold is not None and (cap is None or cap < threshold):
        if cap is None:
            return cap_note, _magnitude(cap_note)
        reason = f"min_market_cap: {cap:g} is below {threshold:g}"
        return reason, _magnitude(reason)
    if lead_type == "debt_raise" and cap:
        need = criterion_threshold(conn, lead_type, "raise_vs_company")
        if need is not None and figure.value is not None:
            pct = 100.0 * figure.value / cap
            if pct < need:
                reason = f"raise_vs_company: {pct:.1f} percent of market cap is below {need:g}"
                return reason, _magnitude(reason)
    growth, compared = _growth_reason(conn, figure, lead_type)
    figure.prior_comparison = compared
    if lead_type in {"capex", "orders_backlog"} and not compared:
        label = "capex_growth_yoy" if lead_type == "capex" else "backlog_qoq_growth"
        reason = f"{label}: no prior comparison in the figures table"
        return reason, _magnitude(reason)
    if growth:
        return growth, _magnitude(growth)
    figure.earlier_similar = _earlier_similar(conn, figure)
    figure.period_age_days = _period_age_days(figure)
    return None, None


def _prefer_spans(classified):
    """One span per capex ticker and figure type. Quarter wins, then ttm."""
    groups = {}
    order = []
    other = []
    for figure, lead_type in classified:
        if lead_type != "capex":
            other.append((figure, lead_type))
            continue
        key = (figure.ticker, figure.figure_type)
        if key not in groups:
            order.append(key)
            groups[key] = []
        groups[key].append((figure, lead_type))
    kept = list(other)
    dropped = []
    for key in order:
        group = groups[key]
        ranks = [_SPAN_RANK.get(figure.instrument, 9) for figure, _lead_type in group]
        if min(ranks) == 9:
            kept.extend(group)
            continue
        best = min(ranks)
        for figure, lead_type in group:
            if _SPAN_RANK.get(figure.instrument, 9) == best:
                kept.append((figure, lead_type))
            else:
                dropped.append((figure, lead_type))
    return kept, dropped


def _latest_periods(classified):
    """Keep the latest period for each ticker, figure type, and instrument."""
    groups = {}
    order = []
    for figure, lead_type in classified:
        key = (figure.ticker, figure.figure_type, figure.instrument)
        if key not in groups:
            order.append(key)
            groups[key] = []
        groups[key].append((figure, lead_type))
    kept = []
    older = []
    for key in order:
        group = groups[key]
        best_end = max(_period_ordinal(figure) for figure, _lead_type in group)
        for figure, lead_type in group:
            if _period_ordinal(figure) == best_end:
                kept.append((figure, lead_type))
            else:
                older.append((figure, lead_type))
    return kept, older


def _period_ordinal(figure):
    end = period_end(figure.period)
    if end is not None:
        return end.toordinal()
    filed = filing_date(figure)
    if not filed:
        return 0
    try:
        return date.fromisoformat(filed).toordinal()
    except ValueError:
        return 0


def _earlier_similar(conn, figure):
    if figure.value in (None, 0):
        return False
    rows = conn.execute(
        """
        SELECT value FROM figures
        WHERE ticker = ? AND figure_type = ? AND instrument = ? AND figure_id != ?
          AND value IS NOT NULL AND value != 0
        """,
        (figure.ticker, figure.figure_type, figure.instrument, figure.figure_id),
    ).fetchall()
    for row in rows:
        gap = abs(row["value"] - figure.value) / abs(figure.value)
        if gap <= SIMILAR_VALUE_BAND:
            return True
    return False


def _period_age_days(figure):
    end = period_end(figure.period)
    if end is None:
        filed = filing_date(figure)
        if not filed:
            return None
        try:
            end = date.fromisoformat(filed)
        except ValueError:
            return None
    anchor = as_of(figure)
    if anchor is None:
        return None
    return (anchor - end).days


def _stake_pct(figure):
    text = figure.value_text or ""
    if "%" in text or figure.figure_type == "stake_pct" or (figure.value is not None and figure.value <= 100):
        return figure.value
    return None


def _outside_window(figure):
    filed = filing_date(figure)
    anchor = as_of(figure)
    if not filed or anchor is None:
        return None
    try:
        filed_on = date.fromisoformat(filed)
    except ValueError:
        return None
    age = (anchor - filed_on).days
    if age > ACCUMULATION_WINDOW_DAYS:
        return f"filing date is outside the {ACCUMULATION_WINDOW_DAYS} day window"
    return None


def _growth_reason(conn, figure, lead_type):
    if lead_type == "capex":
        window = (300, 430)
        need = CAPEX_GROWTH_PCT
        label = "capex_growth_yoy"
    elif lead_type == "orders_backlog":
        window = (70, 120)
        need = BACKLOG_GROWTH_PCT
        label = "backlog_qoq_growth"
    else:
        return None, False
    end = period_end(figure.period)
    if end is None or figure.value is None:
        return None, False
    rows = conn.execute(
        """
        SELECT figure_id, value, period
        FROM figures
        WHERE ticker = ? AND figure_type = ? AND instrument = ? AND figure_id != ?
          AND value IS NOT NULL
        """,
        (figure.ticker, figure.figure_type, figure.instrument, figure.figure_id),
    ).fetchall()
    best = None
    for row in rows:
        prior_end = period_end(row["period"])
        if prior_end is None or row["value"] in (None, 0):
            continue
        days = (end - prior_end).days
        if window[0] <= days <= window[1]:
            distance = abs(days - ((window[0] + window[1]) / 2))
            if best is None or distance < best[0]:
                best = (distance, row["value"])
    if best is None:
        return None, False
    growth = 100.0 * (figure.value - best[1]) / abs(best[1])
    if growth < need:
        return f"below {label} threshold {need:g} percent", True
    return None, True


def _dedupe_batch(candidates):
    groups = {}
    order = []
    for candidate in candidates:
        key = _dedupe_key(candidate)
        if key not in groups:
            order.append(key)
            groups[key] = []
        groups[key].append(candidate)
    kept = []
    rejects = []
    for key in order:
        group = groups[key]
        group.sort(key=_keep_rank, reverse=True)
        kept.append(group[0])
        for loser in group[1:]:
            rejects.append(
                PlannedReject(
                    loser.figure,
                    loser.lead_type,
                    "deterministic",
                    "duplicate in this run",
                    parties=loser.parties,
                )
            )
    return kept, rejects


def _dedupe_key(candidate):
    figure = candidate.figure
    if figure.source_url:
        return (candidate.lead_type, figure.ticker, figure.source_url)
    return (candidate.lead_type, figure.ticker, figure.figure_type, figure.instrument, figure.period)


def _keep_rank(candidate):
    """Higher value, then later filing date, then lower figure id."""
    figure = candidate.figure
    value = figure.value if figure.value is not None else float("-inf")
    filed = figure.announced_on or ""
    return (value, filed, -figure.figure_id)


def _in_flight(conn, candidate):
    figure = candidate.figure
    row = conn.execute(
        """
        SELECT 1
        FROM lead_figures AS link
        JOIN leads ON leads.lead_id = link.lead_id
        WHERE link.figure_id = ? AND leads.status IN ({})
        """.format(", ".join("?" * len(_IN_FLIGHT))),
        (figure.figure_id, *_IN_FLIGHT),
    ).fetchone()
    if row:
        return True
    if not figure.source_url:
        return False
    row = conn.execute(
        """
        SELECT 1 FROM leads
        WHERE ticker = ? AND lead_type = ? AND source_url = ?
          AND status IN ({})
        """.format(", ".join("?" * len(_IN_FLIGHT))),
        (figure.ticker, candidate.lead_type, figure.source_url, *_IN_FLIGHT),
    ).fetchone()
    return row is not None


def _magnitude(reason):
    return Rating(magnitude="small", magnitude_rationale=reason)


def _is_party(text):
    cleaned = (text or "").strip()
    folded = cleaned.casefold()
    if len(cleaned) < 2 or folded in _STOP_PARTIES or folded in _METRIC_LABELS:
        return False
    if "|" in cleaned:
        return False
    if not any(char.isalpha() for char in cleaned):
        return False
    if " " not in cleaned and any(char.islower() for char in cleaned) and any(char.isupper() for char in cleaned[1:]):
        return False
    return True


def _label(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _line(value):
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


def candidate_exchange(candidate):
    return exchange_for(candidate.figure)
