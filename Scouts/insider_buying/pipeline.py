"""Turn parsed purchases into one JSON table and figures-style rows."""

import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from filters import ScreenConfig, screen
from parser import parse_3y, parse_form4


def build_table(documents, config=None):
    config = config or ScreenConfig()
    parsed = []
    for document in documents:
        source = document.get("source")
        if source == "sec":
            parsed.extend(
                parse_form4(
                    document.get("body") or "",
                    document.get("url") or "",
                    document.get("filing_date"),
                    ticker=document.get("ticker"),
                )
            )
        elif source == "asx":
            parsed.extend(
                parse_3y(
                    document.get("body") or "",
                    document.get("url") or "",
                    document.get("filing_date"),
                    document.get("ticker"),
                    company=document.get("company"),
                )
            )
        else:
            from model import record

            parsed.append(
                record(
                    source=source,
                    source_url=document.get("url") or "",
                    filing_date=document.get("filing_date"),
                    flags=["unparseable"],
                    notes=[f"unknown source {source}"],
                )
            )
    records, review = screen(parsed, config)
    return {
        "scout": "insider-buying",
        "schema": 1,
        "config": {
            "min_value": config.min_value,
            "cluster_window_days": config.cluster_window_days,
            "max_trade_age_days": config.max_trade_age_days,
        },
        "records": records,
        "review": review,
        "figures": [to_figure(row) for row in records],
    }


def to_figure(row):
    """One history-db figure. Period includes the insider so same-day buys do not collide."""
    kind = "ceo" if row["is_ceo"] else ""
    shares = row["shares"]
    price = row["price"]
    text = f"{_plain(shares)} shares"
    if price is not None:
        text = f"{text} at {_plain(price)} {row.get('currency') or ''}".strip()
    return {
        "ticker": row.get("ticker"),
        "company": row.get("company"),
        "figure_type": "insider_purchase",
        "instrument": kind,
        "period": f"{row.get('transaction_date') or ''}|{row.get('insider') or ''}",
        "value": row.get("value"),
        "value_text": text,
        "currency": row.get("currency"),
        "seen_at": row.get("filing_date"),
        "announced_on": row.get("filing_date"),
        "source_url": row.get("source_url"),
        "source_type": "asx_announcement" if row.get("source") == "asx" else "filing",
        "verified": 1,
    }


def to_json(table):
    return json.dumps(table, indent=2) + "\n"


def _plain(value):
    if value is None:
        return "?"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)
