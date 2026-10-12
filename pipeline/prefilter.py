"""Deterministic gates: thresholds, then dedupe. No model calls."""

import re
from datetime import date

from pipeline.load import (
    CAPEX_TYPES,
    ORDER_TYPES,
    as_of,
    classify,
    event_date,
    exchange_for,
    extract_cik,
    filing_date,
    period_end,
    period_span_days,
)
from pipeline.marketcap import criterion_threshold, resolve_market_cap
from pipeline.names import normalise_name
from shared.money import format_money

# Rubric and scout defaults the figures can support without a market-cap feed.
# Debt uses the scout's minimum deal size. Insider dollars, stake percent,
# capex year-over-year, backlog quarter-over-quarter, and the accumulation
# window match rubric version 1.
INSIDER_MIN_DOLLARS = 10_000
STAKE_MIN_PCT = 5
DEBT_MIN_DOLLARS = 50_000_000
CAPEX_GROWTH_PCT = 10
BACKLOG_GROWTH_PCT = 10
IMPLAUSIBLE_RATIO = 0.10
ACCUMULATION_WINDOW_DAYS = 90
STALE_PERIOD_DAYS = 120
SIMILAR_VALUE_BAND = 0.20
# A year-ago quarter is the same span whose period ends 365±10 days earlier.
YOY_WINDOW = (355, 375, 365)
# A prior quarter-end is about 90 days earlier. Fiscal quarters vary.
QUARTER_WINDOW = (70, 120, 90)
SPAN_SLACK_DAYS = 15
# Same dollar bar the stub uses for a large orders figure.
ORDER_LARGE_VALUE = 100_000_000
_COMPANYFACTS_BACKLOG = {
    "RevenueRemainingPerformanceObligation",
    "BacklogOfOrders",
}
_SPAN_RANK = {"quarter": 0, "ttm": 1, "ytd": 2, "annual": 3, "instant": 4}
_METRIC_LABELS = {"orders", "order", "backlog", "revenue"}
_COMPANY_TOTALS = _METRIC_LABELS | {
    "",
    "revenueremainingperformanceobligation",
    "backlogoforders",
}

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


def prefilter(conn, figures, limit=None, now=None):
    """Return (candidates, rejects, used) from the latest figures.

    `limit` keeps the first N lead figures in newest-announced order before
    the gates run. `used` is that count: lead figures after the limit, not
    the rows loaded before it. Balance-sheet tags that are not a lead type
    are skipped and are not rejects, and they do not consume `limit`. Older
    periods and extra capex spans are not rated.
    """
    ordered = [figure for figure in figures if classify(figure.scout, figure.figure_type) is not None]
    if limit is not None:
        ordered = ordered[: int(limit)]
    used = len(ordered)
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
    classified = _aggregate_insider_lots(classified)
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
        reason, rating = _threshold(conn, figure, lead_type, now=now)
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
    return kept, rejects, used


def interesting(rating, *, ignore_on_time=False):
    """A lead is emitted when the rating is not rejected.

    One routine novelty, or one on_time timing, still emits the lead.
    Two failed ratings reject it, and so does a hard failure on its own.
    Quarter-end capex and backlog pass ignore_on_time, so that band is not a failure.
    """
    return failure_reason(rating, ignore_on_time=ignore_on_time) is None


def failure_reason(rating, *, ignore_on_time=False):
    """Why the rating is rejected, or None when the lead survives.

    magnitude small, attention noticed, and timing late reject on their own.
    novelty routine and timing on_time are surviving issues when exactly one
    rating fails. Two or more failed ratings reject, including routine together
    with on_time. For a quarter-end capex or backlog figure, on_time is not
    an issue at all.
    """
    hard, soft = _split_rating(rating, ignore_on_time=ignore_on_time)
    failed = hard + soft
    if hard or len(failed) >= 2:
        return "; ".join(failed)
    return None


def surviving_issue(rating, *, ignore_on_time=False):
    """The one soft failure that stays on the lead, or None when nothing does."""
    hard, soft = _split_rating(rating, ignore_on_time=ignore_on_time)
    if hard or len(soft) != 1:
        return None
    return soft[0]


def _split_rating(rating, ignore_on_time=False):
    if rating is None or not rating.complete():
        return ["significance response was incomplete"], []
    hard = []
    soft = []
    if rating.magnitude not in {"large", "moderate"}:
        hard.append(_rating_problem("magnitude", rating.magnitude, rating.magnitude_rationale))
    if rating.novelty == "routine":
        soft.append(_rating_problem("novelty", rating.novelty, rating.novelty_rationale))
    elif rating.novelty != "first":
        hard.append(_rating_problem("novelty", rating.novelty, rating.novelty_rationale))
    if rating.attention != "unnoticed":
        hard.append(_rating_problem("attention", rating.attention, rating.attention_rationale))
    if rating.timing == "on_time":
        if not ignore_on_time:
            soft.append(_rating_problem("timing", rating.timing, rating.timing_rationale))
    elif rating.timing != "early":
        hard.append(_rating_problem("timing", rating.timing, rating.timing_rationale))
    return hard, soft


def _rating_problem(name, label, rationale):
    return f"{name} {label}: {rationale}"


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
            add("holder", parts[-1])
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


def _threshold(conn, figure, lead_type, now=None):
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
    implausible = _implausible_total(conn, figure, lead_type)
    if implausible:
        return implausible, _magnitude(implausible)
    cap, _cap_note = resolve_market_cap(conn, figure.ticker)
    figure.market_cap = cap
    threshold = criterion_threshold(conn, lead_type, "min_market_cap")
    figure.market_cap_unverified = cap is None and threshold is not None
    if threshold is not None and cap is not None and cap < threshold:
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
        if getattr(figure, "growth_unverified", False):
            pass
        else:
            label = "capex_growth_yoy" if lead_type == "capex" else "backlog_qoq_growth"
            reason = f"{label}: no prior comparison in the figures table"
            return reason, _magnitude(reason)
    if growth:
        return growth, _magnitude(growth)
    figure.earlier_similar = _earlier_similar(conn, figure)
    figure.earlier_type = _earlier_type(conn, figure)
    figure.period_age_days = _period_age_days(figure, now)
    return None, None


def _is_company_total(instrument):
    text = instrument or ""
    if ":" in text:
        return False
    return text.casefold() in _COMPANY_TOTALS


def _implausible_total(conn, figure, lead_type):
    """Reject a company-total backlog far below the company's other periods.

    Segment rows (an instrument containing `:`) and named counterparties
    are not company totals. The comparison uses every other company-total
    orders figure for the ticker, including periods outside the load window.
    """
    if lead_type != "orders_backlog" or not _is_company_total(figure.instrument):
        return None
    if figure.value is None or figure.value <= 0:
        return None
    rows = conn.execute(
        """
        SELECT figure_id, figure_type, instrument, value
        FROM figures
        WHERE ticker = ? AND figure_id != ?
          AND value IS NOT NULL AND value > 0
        """,
        (figure.ticker, figure.figure_id),
    ).fetchall()
    ceiling = 0.0
    for row in rows:
        if row["figure_type"] not in ORDER_TYPES:
            continue
        if not _is_company_total(row["instrument"] or ""):
            continue
        ceiling = max(ceiling, float(row["value"]))
    # 10% exactly is kept. Scale by 100 so the boundary does not depend on binary fractions.
    if ceiling and figure.value * 100 < ceiling * round(IMPLAUSIBLE_RATIO * 100):
        return f"implausible backlog: {figure.value:g} is below 10% of {ceiling:g}"
    return None


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
    """True when a different period stores a value within the similar band.

    A later scout run stores the same figure again. That copy shares the
    period and the value, so it is not an earlier occurrence. A restatement
    of the same period is skipped too. A different period inside the band,
    including the same value, is routine. Lots by the same insider on the
    same day are one purchase, not an earlier print.
    """
    if figure.value in (None, 0):
        return False
    rows = conn.execute(
        """
        SELECT value, period, instrument FROM figures
        WHERE ticker = ? AND figure_type = ? AND instrument = ? AND figure_id != ?
          AND value IS NOT NULL AND value != 0
        """,
        (figure.ticker, figure.figure_type, figure.instrument, figure.figure_id),
    ).fetchall()
    period = figure.period or ""
    identity = _purchase_key(figure.ticker, figure.instrument, period)
    for row in rows:
        if (row["period"] or "") == period:
            continue
        if identity is not None and identity == _purchase_key(figure.ticker, row["instrument"], row["period"]):
            continue
        gap = abs(row["value"] - figure.value) / abs(figure.value)
        if gap <= SIMILAR_VALUE_BAND:
            return True
    return False


def _period_age_days(figure, today=None):
    """Days from the event date to the sweep date.

    The event date is the insider trade date, otherwise the period end,
    otherwise the filing date. `seen_at` is when the figure was stored, not
    when the event happened.
    """
    event = event_date(figure)
    if event is None:
        return None
    if today is None:
        from shared.store import utc_now

        today = utc_now()
    try:
        anchor = date.fromisoformat(str(today)[:10])
    except ValueError:
        return None
    return (anchor - event).days


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
    """Compare a figure with an earlier one of the same span.

    Capex matches a period that ends 365±10 days earlier. The instrument
    string and the period's exact formatting do not have to match, and a
    different capex tag still counts. When that quarter is missing, capex
    uses the trailing-twelve-month total against the total from a year
    earlier. Backlog matches a prior quarter-end or a year-ago end,
    including companyfacts history stored under another instrument. A large
    order with none of those stays in the sweep as an open growth question
    instead of a reject.
    """
    if lead_type == "capex":
        windows = (YOY_WINDOW,)
        need = CAPEX_GROWTH_PCT
        label = "capex_growth_yoy"
        require_span = True
    elif lead_type == "orders_backlog":
        windows = (QUARTER_WINDOW, YOY_WINDOW)
        need = BACKLOG_GROWTH_PCT
        label = "backlog_qoq_growth"
        require_span = False
    else:
        return None, False
    end = period_end(figure.period)
    if end is None or figure.value is None:
        return None, False
    rows = conn.execute(
        """
        SELECT figure_id, value, period, figure_type, source_type, instrument
        FROM figures
        WHERE ticker = ? AND figure_id != ? AND value IS NOT NULL AND value != 0
        """,
        (figure.ticker, figure.figure_id),
    ).fetchall()
    span = period_span_days(figure.period)
    best = _pick_prior(rows, lead_type, end, span, windows, require_span)
    if best is None and lead_type == "orders_backlog" and figure.value >= ORDER_LARGE_VALUE:
        figure.growth_unverified = True
        return None, False
    if best is None and lead_type == "capex":
        pair = _trailing_yoy(rows, end)
        if pair is not None:
            current_value, current_period, prior_value, prior_period = pair
            growth = 100.0 * (current_value - prior_value) / abs(prior_value)
            _remember_growth(figure, current_period, current_value, prior_period, prior_value, growth)
            if growth < need:
                return f"below {label} threshold {need:g} percent", True
            return None, True
    if best is None:
        return None, False
    prior_value, prior_period = best
    growth = 100.0 * (figure.value - prior_value) / abs(prior_value)
    _remember_growth(figure, figure.period, figure.value, prior_period, prior_value, growth)
    if growth < need:
        return f"below {label} threshold {need:g} percent", True
    return None, True


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_PERIOD_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _remember_growth(figure, current_period, current_value, prior_period, prior_value, growth):
    """Keep the comparison the criteria line and the thesis both quote."""
    figure.growth_pct = growth
    figure.growth_text = _growth_sentence(
        current_period,
        current_value,
        prior_period,
        prior_value,
        growth,
        figure.currency,
    )


def _growth_sentence(current_period, current_value, prior_period, prior_value, growth, currency):
    pct = int(round(growth))
    sign = "+" if pct > 0 else ""
    return (
        f"{_period_label(current_period)} {format_money(current_value, currency)} vs "
        f"{_period_label(prior_period)} {format_money(prior_value, currency)}, {sign}{pct}%"
    )


def _period_label(period):
    """Jun-Aug 2026 for a same-year span. A single end date is Jun 2026."""
    text = period or ""
    found = _PERIOD_DATE.findall(text)
    if "|" in text and "/" not in text:
        found = found[:1]
    if not found:
        return text or "period"
    try:
        dates = [date.fromisoformat(item) for item in found]
    except ValueError:
        return text
    if len(dates) >= 2:
        start, end = dates[0], dates[-1]
        if start.year == end.year and start.month != end.month:
            return f"{_MONTHS[start.month - 1]}-{_MONTHS[end.month - 1]} {end.year}"
        if start.year != end.year:
            return f"{_MONTHS[start.month - 1]} {start.year}-{_MONTHS[end.month - 1]} {end.year}"
        return f"{_MONTHS[end.month - 1]} {end.year}"
    end = dates[-1]
    return f"{_MONTHS[end.month - 1]} {end.year}"


def _pick_prior(rows, lead_type, end, span, windows, require_span):
    best = None
    for row in rows:
        if not _growth_family(row, lead_type):
            continue
        prior_end = period_end(row["period"])
        if prior_end is None:
            continue
        prior_span = period_span_days(row["period"])
        if require_span:
            if span is None or prior_span is None or abs(span - prior_span) > SPAN_SLACK_DAYS:
                continue
        elif span is not None and prior_span is not None and abs(span - prior_span) > SPAN_SLACK_DAYS:
            continue
        days = (end - prior_end).days
        for low, high, target in windows:
            if not (low <= days <= high):
                continue
            # A prior quarter outranks a year-ago row when both exist.
            rank = (0 if target < 200 else 1, abs(days - target))
            if best is None or rank < best[0]:
                best = (rank, row["value"], row["period"])
    if best is None:
        return None
    return best[1], best[2]


def _growth_family(row, lead_type):
    figure_type = row["figure_type"] or ""
    if lead_type == "capex":
        return figure_type in CAPEX_TYPES
    if figure_type in ORDER_TYPES or figure_type in _COMPANYFACTS_BACKLOG:
        return True
    return (row["source_type"] or "") == "xbrl_company_facts"


def _trailing_yoy(rows, end):
    """Current trailing-twelve-month total and the one a year earlier.

    Used when the year-ago single quarter is not stored. The current total
    ends on the candidate's period end (a few days of slack). The prior total
    ends 365±10 days before that.
    """
    totals = []
    for row in rows:
        if not _growth_family(row, "capex") or not _is_trailing_twelve(row):
            continue
        prior_end = period_end(row["period"])
        if prior_end is None or row["value"] in (None, 0):
            continue
        totals.append((prior_end, row["value"], row["period"]))
    current = None
    for prior_end, value, period in totals:
        gap = (end - prior_end).days
        if not (-15 <= gap <= 45):
            continue
        rank = abs(gap)
        if current is None or rank < current[0]:
            current = (rank, prior_end, value, period)
    if current is None:
        return None
    _, current_end, current_value, current_period = current
    prior = None
    low, high, target = YOY_WINDOW
    for prior_end, value, period in totals:
        days = (current_end - prior_end).days
        if not (low <= days <= high):
            continue
        rank = abs(days - target)
        if prior is None or rank < prior[0]:
            prior = (rank, value, period)
    if prior is None:
        return None
    return current_value, current_period, prior[1], prior[2]


def _is_trailing_twelve(row):
    instrument = (row["instrument"] or "").casefold()
    if instrument == "ttm":
        return True
    if instrument in {"annual", "quarter", "ytd", "instant"}:
        return False
    span = period_span_days(row["period"])
    return span is not None and 350 <= span <= 380


def _earlier_type(conn, figure):
    """True when this ticker already has this figure type in another period.

    A re-stored copy of the same period is not an earlier figure. Another lot
    by the same insider on the same day is not an earlier figure either.
    """
    rows = conn.execute(
        """
        SELECT period, instrument FROM figures
        WHERE ticker = ? AND figure_type = ? AND figure_id != ?
        """,
        (figure.ticker, figure.figure_type, figure.figure_id),
    ).fetchall()
    period = figure.period or ""
    identity = _purchase_key(figure.ticker, figure.instrument, period)
    for row in rows:
        if (row["period"] or "") == period:
            continue
        if identity is not None and identity == _purchase_key(figure.ticker, row["instrument"], row["period"]):
            continue
        return True
    return False


def _aggregate_insider_lots(classified):
    """Sum lots by the same insider, ticker, and trade day into one figure.

    The scout used to suffix the period (`date|name|2`) so each lot kept a
    figures key. Those rows then marked each other routine. The dollars are
    one purchase. The primary figure keeps every lot id for the lead.
    """
    groups = {}
    order = []
    other = []
    for figure, lead_type in classified:
        key = None
        if lead_type == "insider_buying":
            key = _lot_group_key(figure)
        if key is None:
            other.append((figure, lead_type))
            continue
        if key not in groups:
            order.append(key)
            groups[key] = []
        groups[key].append(figure)
    kept = list(other)
    for key in order:
        lots = groups[key]
        if len(lots) == 1:
            kept.append((lots[0], "insider_buying"))
            continue
        primary = min(lots, key=_lot_keep_rank)
        total = sum(lot.value or 0 for lot in lots)
        primary.value = total
        primary.value_text = f"{len(lots)} lots totalling {format_money(total, primary.currency)}"
        primary.aggregated_ids = [lot.figure_id for lot in lots if lot.figure_id != primary.figure_id]
        kept.append((primary, "insider_buying"))
    return kept


def _lot_group_key(figure):
    day, insider = _day_and_insider(figure.period)
    if not day or not insider:
        return None
    return (
        (figure.ticker or "").upper(),
        (figure.instrument or "").casefold(),
        (figure.currency or "").upper(),
        day,
        insider,
    )


def _lot_keep_rank(figure):
    """Unsuffixed period, then the larger lot, then the lower figure id."""
    suffix = 1 if re.search(r"\|\d+$", figure.period or "") else 0
    value = figure.value if figure.value is not None else float("-inf")
    return (suffix, -value, figure.figure_id)


def _purchase_key(ticker, instrument, period):
    day, insider = _day_and_insider(period)
    if not day or not insider:
        return None
    return ((ticker or "").upper(), (instrument or "").casefold(), day, insider)


def _day_and_insider(period):
    """Trade day and insider from `date|name` or `date|name|2`."""
    text = period or ""
    if "|" not in text:
        return "", ""
    day, _, rest = text.partition("|")
    if len(day) != 10 or day[4] != "-" or day[7] != "-":
        return "", ""
    parts = rest.split("|")
    if len(parts) >= 2 and parts[-1].isdigit():
        insider = "|".join(parts[:-1])
    else:
        insider = rest
    insider = insider.strip().casefold()
    if not insider:
        return "", ""
    return day, insider


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
