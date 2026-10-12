"""Current scout figures, with earlier runs kept as comparison history."""

import logging
import re
from datetime import date

log = logging.getLogger(__name__)

SCOUT_LEAD_TYPES = {
    "capex": "capex",
    "debt_raise": "debt_raise",
    "debt-raise": "debt_raise",
    "orders_backlog": "orders_backlog",
    "orders-backlog": "orders_backlog",
    "insider_buying": "insider_buying",
    "insider-buying": "insider_buying",
    "accumulation": "accumulation",
}

CAPEX_TYPES = frozenset(
    {
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
        "PaymentsForCapitalImprovements",
        "PaymentsToAcquireOtherPropertyPlantAndEquipment",
        "capex_guidance",
        "capex_announcement",
    }
)
DEBT_TYPES = frozenset({"bond_amount", "facility_amount", "loan_amount", "proceeds_amount"})
ORDER_TYPES = frozenset({"customer_backlog", "supplier_order", "backlog"})
ACCUMULATION_TYPES = frozenset({"substantial_holding", "stake_pct"})
INSIDER_TYPES = frozenset({"insider_purchase"})

_TYPES_FOR_LEAD = {
    "capex": CAPEX_TYPES,
    "debt_raise": DEBT_TYPES,
    "orders_backlog": ORDER_TYPES,
    "accumulation": ACCUMULATION_TYPES,
    "insider_buying": INSIDER_TYPES,
}

# Callers may still pass window_days. The candidate set is not limited to it.
FIGURE_WINDOW_DAYS = 30
INSIDER_TRADE_MAX_AGE_DAYS = 30

_ACCESSION = re.compile(r"\d{10}-\d{2}-\d{6}")
_CIK = re.compile(r"/data/(\d+)/")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


class Figure:
    def __init__(self, row):
        self.figure_id = row["figure_id"]
        self.run_id = row["run_id"]
        self.scout = row["scout"]
        self.ticker = row["ticker"]
        self.company = row["company"]
        self.figure_type = row["figure_type"]
        self.instrument = row["instrument"] or ""
        self.period = row["period"] or ""
        self.value = row["value"]
        self.value_text = row["value_text"] or ""
        self.currency = row["currency"]
        self.seen_at = row["seen_at"] or ""
        self.announced_on = row["announced_on"]
        self.source_url = row["source_url"] or ""
        self.source_type = row["source_type"] or ""
        self.verified = int(row["verified"])
        self.market_cap = None
        self.market_cap_unverified = False

    @property
    def lead_type(self):
        return classify(self.scout, self.figure_type)


def classify(scout, figure_type):
    """Lead type when this figure is a candidate, else None."""
    lead_type = SCOUT_LEAD_TYPES.get((scout or "").strip())
    if lead_type is None:
        return None
    allowed = _TYPES_FOR_LEAD.get(lead_type)
    if figure_type not in allowed:
        return None
    return lead_type


def load_latest_figures(conn, *, as_of=None, window_days=FIGURE_WINDOW_DAYS):
    """Current candidates for each scout, newest run first.

    A run with no scope is the full universe: its figures are the candidates
    for every ticker a newer scoped run did not already cover, and older runs
    are comparison history only. A `--tickers` scope replaces only those
    tickers, including instruments that the scoped run no longer stores.
    Identical figures (ticker, type, instrument, period, value) collapse to
    the newest row. An insider trade more than 30 days before its filing date
    is dropped. `as_of` and `window_days` do not limit this set.
    """
    _ = (as_of, window_days)
    scouts = tuple(SCOUT_LEAD_TYPES)
    placeholders = ", ".join("?" * len(scouts))
    runs = conn.execute(
        f"""
        SELECT run_id, scout, started_at, scope
        FROM runs
        WHERE scout IN ({placeholders})
        ORDER BY started_at DESC, run_id DESC
        """,
        scouts,
    ).fetchall()
    if not runs:
        return []
    run_ids = [row["run_id"] for row in runs]
    id_marks = ", ".join("?" * len(run_ids))
    stored = conn.execute(
        f"""
        SELECT f.figure_id, f.run_id, f.ticker, f.company, f.figure_type,
               f.instrument, f.period, f.value, f.value_text, f.currency,
               f.seen_at, f.announced_on, f.source_url, f.source_type, f.verified,
               r.scout AS scout, r.started_at AS started_at
        FROM figures AS f
        JOIN runs AS r ON r.run_id = f.run_id
        WHERE f.run_id IN ({id_marks})
        """,
        run_ids,
    ).fetchall()
    by_run = {}
    for row in stored:
        by_run.setdefault(row["run_id"], []).append(row)
    selected = []
    by_scout = {}
    for run in runs:
        by_scout.setdefault(run["scout"], []).append(run)
    for scout_runs in by_scout.values():
        covered = set()
        for run in scout_runs:
            tokens = _scope_tokens(run["scope"])
            for row in by_run.get(run["run_id"], []):
                if tokens is not None and not _ticker_in(row["ticker"], tokens):
                    continue
                if _ticker_in(row["ticker"], covered):
                    continue
                selected.append(row)
            if tokens is None:
                break
            covered |= _expand_tickers(tokens)
    collapsed = _collapse_identical(selected)
    figures = []
    for row in collapsed:
        figure = Figure(row)
        if _stale_insider(figure):
            log.warning(
                "skipped insider trade ticker=%s period=%s announced_on=%s: "
                "trade is more than %s days before the filing",
                figure.ticker,
                figure.period,
                figure.announced_on or "",
                INSIDER_TRADE_MAX_AGE_DAYS,
            )
            continue
        figures.append(figure)
    figures.sort(key=lambda figure: (figure.announced_on or "", figure.figure_id), reverse=True)
    return figures


def _scope_tokens(scope):
    if scope is None:
        return None
    text = str(scope).strip()
    if not text or text.casefold() == "all":
        return None
    tokens = _expand_tickers(part for part in text.split(",") if part.strip())
    return tokens or None


def _expand_tickers(tickers):
    expanded = set()
    for ticker in tickers:
        full = str(ticker).strip().upper()
        if not full:
            continue
        bare = full[:-3] if full.endswith(".AX") else full
        expanded.add(full)
        expanded.add(bare)
        if bare:
            expanded.add(f"{bare}.AX")
    return expanded


def _ticker_in(ticker, tokens):
    if not tokens:
        return False
    return bool(_expand_tickers([ticker]) & set(tokens))


def _collapse_identical(rows):
    """One row per ticker, type, instrument, period, and value. Newest wins."""
    best = {}
    for row in rows:
        key = (
            row["ticker"],
            row["figure_type"],
            row["instrument"] or "",
            row["period"] or "",
            row["value"],
        )
        current = best.get(key)
        rank = (row["started_at"] or "", row["figure_id"] or 0)
        if current is None or rank > current[0]:
            best[key] = (rank, row)
    return [item[1] for item in best.values()]


def _stale_insider(figure):
    """True when an insider trade date is more than 30 days before the filing.

    The filing date is the clock, matching the insider scout: a late Form 4
    can arrive long after the trade. A missing date is kept.
    """
    if figure.figure_type != "insider_purchase" and classify(figure.scout, figure.figure_type) != "insider_buying":
        return False
    dates = _DATE.findall(figure.period or "")
    if not dates:
        return False
    filed = filing_date(figure)
    if not filed:
        return False
    try:
        traded = date.fromisoformat(dates[0])
        filed_on = date.fromisoformat(filed)
    except ValueError:
        return False
    return (filed_on - traded).days > INSIDER_TRADE_MAX_AGE_DAYS


def exchange_for(figure):
    if figure.source_type == "asx_announcement" or (figure.ticker or "").endswith(".AX"):
        return "ASX"
    return "US"


def extract_cik(url):
    match = _CIK.search(url or "")
    if not match:
        return None
    return str(int(match.group(1)))


def extract_accession(figure):
    for text in (figure.period, figure.instrument, figure.source_url, figure.value_text):
        match = _ACCESSION.search(text or "")
        if match:
            return match.group(0)
    return None


def period_end(period):
    """The period's end date.

    A slash span (`2025-07-01/2025-09-30`) ends on the last date. A
    companyfacts row stored as `end|filed` ends on the first date; the
    second date is when it was filed.
    """
    text = period or ""
    found = _DATE.findall(text)
    if not found:
        return None
    chosen = found[0] if "|" in text and "/" not in text else found[-1]
    try:
        return date.fromisoformat(chosen)
    except ValueError:
        return None


def is_quarter_figure(figure, lead_type=None):
    """True for a single-quarter print or a quarter-end backlog balance.

    Capex stores that print with instrument `quarter` or a span of about
    ninety days. A backlog balance with no duration is the stock at a
    period end. A one-day supplier order is not.
    """
    instrument = (getattr(figure, "instrument", "") or "").casefold()
    if instrument == "quarter":
        return True
    period = getattr(figure, "period", "") or ""
    span = period_span_days(period)
    if span is not None and 80 <= span <= 115:
        return True
    kind = lead_type or classify(getattr(figure, "scout", ""), getattr(figure, "figure_type", ""))
    figure_type = getattr(figure, "figure_type", "") or ""
    if kind != "orders_backlog" or figure_type not in {"backlog", "customer_backlog"} or span is not None:
        return False
    end = period_end(period)
    return end is not None and end.day >= 28


def period_span_days(period):
    """Days covered by a start/end span.

    A single date has none. A companyfacts `end|filed` row has none: the
    second date is the filing date. Two dates written without a slash
    (`from 2025-07-01 to 2025-09-30`) still count.
    """
    text = period or ""
    if "|" in text and "/" not in text:
        return None
    found = _DATE.findall(text)
    if len(found) < 2:
        return None
    try:
        start = date.fromisoformat(found[0])
        end = date.fromisoformat(found[-1])
    except ValueError:
        return None
    days = (end - start).days
    if days < 0:
        return None
    return days


def as_of(figure):
    try:
        return date.fromisoformat((figure.seen_at or "")[:10])
    except ValueError:
        return None


def filing_date(figure):
    raw = figure.announced_on or ""
    if not raw:
        return None
    return raw[:10]


def event_date(figure):
    """Insider trade date, otherwise the period end, otherwise the filing date."""
    dates = _DATE.findall(figure.period or "")
    insider = figure.figure_type == "insider_purchase" or classify(figure.scout, figure.figure_type) == "insider_buying"
    if insider and dates:
        try:
            return date.fromisoformat(dates[0])
        except ValueError:
            pass
    end = period_end(figure.period)
    if end is not None:
        return end
    filed = filing_date(figure)
    if not filed:
        return None
    try:
        return date.fromisoformat(filed)
    except ValueError:
        return None
