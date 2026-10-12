"""Latest figure from each scout, across a recent window of runs."""

import re
from datetime import date, timedelta

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

FIGURE_WINDOW_DAYS = 30

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
    """Latest figure per ticker, type, instrument, and period for each scout.

    The window is `window_days` back from `as_of` (a UTC timestamp or date).
    A later run that names only some tickers does not hide figures from an
    earlier run that started inside the window. Period stays in the key so a
    prior quarter is still loaded for the growth comparison. The same key in
    two runs keeps the later `started_at`, then the higher figure id.
    """
    cutoff = _window_start(as_of, window_days)
    scouts = tuple(SCOUT_LEAD_TYPES)
    placeholders = ", ".join("?" * len(scouts))
    rows = conn.execute(
        f"""
        WITH recent AS (
            SELECT run_id, scout, started_at
            FROM runs
            WHERE scout IN ({placeholders})
              AND started_at >= ?
        ),
        ranked AS (
            SELECT f.figure_id,
                   ROW_NUMBER() OVER (
                       PARTITION BY recent.scout, f.ticker, f.figure_type, f.instrument, f.period
                       ORDER BY recent.started_at DESC, f.figure_id DESC
                   ) AS n
            FROM figures AS f
            JOIN recent ON recent.run_id = f.run_id
        )
        SELECT f.figure_id, f.run_id, f.ticker, f.company, f.figure_type,
               f.instrument, f.period, f.value, f.value_text, f.currency,
               f.seen_at, f.announced_on, f.source_url, f.source_type, f.verified,
               recent.scout AS scout
        FROM figures AS f
        JOIN recent ON recent.run_id = f.run_id
        JOIN ranked ON ranked.figure_id = f.figure_id AND ranked.n = 1
        ORDER BY COALESCE(f.announced_on, '') DESC, f.figure_id DESC
        """,
        (*scouts, cutoff),
    ).fetchall()
    return [Figure(row) for row in rows]


def _window_start(as_of, window_days):
    if as_of is None:
        from shared.store import utc_now

        as_of = utc_now()
    try:
        day = date.fromisoformat(str(as_of)[:10])
    except ValueError:
        day = date.today()
    return (day - timedelta(days=int(window_days))).isoformat()


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
    found = _DATE.findall(period or "")
    if not found:
        return None
    try:
        return date.fromisoformat(found[-1])
    except ValueError:
        return None


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
