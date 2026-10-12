"""Latest figure from each scout run."""

import re
from datetime import date

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


def load_latest_figures(conn):
    """Figures from the latest run of each scout, oldest run id losing a tie."""
    scouts = tuple(SCOUT_LEAD_TYPES)
    placeholders = ", ".join("?" * len(scouts))
    rows = conn.execute(
        f"""
        WITH ranked AS (
            SELECT run_id, scout, started_at,
                   ROW_NUMBER() OVER (
                       PARTITION BY scout
                       ORDER BY started_at DESC, run_id DESC
                   ) AS n
            FROM runs
            WHERE scout IN ({placeholders})
        )
        SELECT f.figure_id, f.run_id, f.ticker, f.company, f.figure_type,
               f.instrument, f.period, f.value, f.value_text, f.currency,
               f.seen_at, f.announced_on, f.source_url, f.source_type, f.verified,
               ranked.scout AS scout
        FROM figures AS f
        JOIN ranked ON ranked.run_id = f.run_id AND ranked.n = 1
        ORDER BY COALESCE(f.announced_on, '') DESC, f.figure_id DESC
        """,
        scouts,
    ).fetchall()
    return [Figure(row) for row in rows]


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
