"""One record shape for customer backlog and supplier orders."""

RECORD_KEYS = (
    "source_side",
    "company",
    "ticker",
    "counterparty",
    "equipment_or_period",
    "value",
    "currency",
    "timing",
    "capacity",
    "source_url",
    "filing_date",
    "source_type",
    "disclosure",
    "flags",
    "notes",
)


def record(
    source_side,
    company,
    source_url,
    filing_date,
    source_type,
    disclosure,
    ticker=None,
    counterparty=None,
    equipment_or_period=None,
    value=None,
    currency=None,
    timing=None,
    capacity=None,
    flags=None,
    notes=None,
):
    row = {
        "source_side": source_side,
        "company": company,
        "ticker": ticker,
        "counterparty": counterparty,
        "equipment_or_period": equipment_or_period,
        "value": value,
        "currency": currency,
        "timing": timing,
        "capacity": capacity,
        "source_url": source_url,
        "filing_date": filing_date,
        "source_type": source_type,
        "disclosure": disclosure,
        "flags": list(flags or []),
        "notes": list(notes or []),
    }
    missing = [key for key in RECORD_KEYS if key not in row]
    if missing:
        raise KeyError(",".join(missing))
    return row


def archive_index_url(cik, accession):
    cik_path = str(int(str(cik).lstrip("0") or "0"))
    acc = (accession or "").strip()
    nodash = acc.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik_path}/{nodash}/{acc}-index.html"
