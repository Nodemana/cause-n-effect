"""One record shape for SEC Form 4 and ASX Appendix 3Y purchases."""

RECORD_KEYS = (
    "insider",
    "role",
    "is_ceo",
    "company",
    "ticker",
    "shares",
    "price",
    "value",
    "currency",
    "transaction_date",
    "filing_date",
    "source_url",
    "source",
    "form",
    "transaction_code",
    "open_market",
    "cluster",
    "flags",
    "notes",
)


def record(**fields):
    row = {key: None for key in RECORD_KEYS}
    row["is_ceo"] = False
    row["open_market"] = False
    row["cluster"] = False
    row["flags"] = []
    row["notes"] = []
    row["source"] = fields.get("source")
    row["form"] = fields.get("form")
    for key, value in fields.items():
        if key not in row:
            raise KeyError(key)
        row[key] = value
    row["flags"] = list(row["flags"] or [])
    row["notes"] = list(row["notes"] or [])
    row["is_ceo"] = bool(row["is_ceo"])
    row["open_market"] = bool(row["open_market"])
    row["cluster"] = bool(row["cluster"])
    return row
