"""Turn capex facts, guidance hits, and ASX headlines into shared figures rows."""

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[1]
for _entry in (str(_REPO), str(_HERE)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from money import first_amount  # noqa: E402
from shared.store import connect, dedupe_figures, save_run  # noqa: E402


def run(db_path, facts, guidance, announcements, started_at, seen_at, notes=None):
    """Write one capex run. `facts`, `guidance`, and `announcements` come from the fetcher."""
    figures = []
    figures.extend(figures_from_facts(facts, seen_at))
    figures.extend(figures_from_guidance(guidance, seen_at))
    figures.extend(figures_from_asx(announcements, seen_at))
    figures = dedupe_figures(figures)
    run_id = f"{started_at[:10]}-capex"
    conn = connect(db_path)
    try:
        save_run(conn, run_id, "capex", started_at, figures, notes=notes)
    finally:
        conn.close()
    return {"run_id": run_id, "figures": len(figures)}


def figures_from_facts(bundles, seen_at):
    rows = []
    for bundle in bundles:
        cik = bundle["cik"]
        for fact in bundle["facts"]:
            row = fact["row"]
            start = row.get("start")
            end = row["end"]
            period = f"{start}/{end}" if start else end
            accn = row.get("accn") or ""
            rows.append(
                {
                    "ticker": bundle["ticker"],
                    "company": bundle.get("company"),
                    "figure_type": fact["tag"],
                    "instrument": "",
                    "period": period,
                    "value": row.get("val"),
                    "value_text": _plain(row.get("val")),
                    "currency": (fact.get("unit") or "").upper(),
                    "seen_at": seen_at,
                    "announced_on": (row.get("filed") or "")[:10] or None,
                    "source_url": _archive(cik, accn) if accn else None,
                    "source_type": "filing",
                    "verified": 1,
                    "start": start,
                    "end": end,
                    "frame": row.get("frame"),
                    "fp": row.get("fp"),
                }
            )
    return rows


def figures_from_guidance(hits, seen_at):
    rows = []
    for item in hits:
        hit = item["hit"]
        source = hit.get("_source") or {}
        accession, document = _hit_id(hit)
        filed = str(source.get("file_date") or "")[:10]
        period = str(source.get("period_ending") or filed or "")[:10]
        description = (source.get("file_description") or "").strip()
        amount = first_amount(description, dollar="USD")
        form = source.get("form") or ""
        cik = item.get("cik") or _first_cik(source)
        rows.append(
            {
                "ticker": item["ticker"],
                "company": item.get("company") or _display_name(source),
                "figure_type": "capex_guidance",
                "instrument": accession or form,
                "period": period,
                "value": None if amount is None else amount["value"],
                "value_text": amount["text"] if amount else description or None,
                "currency": None if amount is None else amount["currency"],
                "seen_at": seen_at,
                "announced_on": filed or None,
                "source_url": _archive(cik, accession, document) if accession and cik else None,
                "source_type": "filing",
                "verified": 1,
            }
        )
    return rows


def figures_from_asx(announcements, seen_at):
    rows = []
    for item in announcements:
        amount = first_amount(item.get("headline") or "", dollar="AUD")
        rows.append(
            {
                "ticker": item["ticker"],
                "company": item.get("company"),
                "figure_type": "capex_announcement",
                "instrument": item.get("document_key") or "",
                "period": item.get("filed") or "",
                "value": None if amount is None else amount["value"],
                "value_text": amount["text"] if amount else item.get("headline"),
                "currency": None if amount is None else amount["currency"],
                "seen_at": seen_at,
                "announced_on": item.get("filed"),
                "source_url": item.get("source_url"),
                "source_type": "asx_announcement",
                "verified": 1,
            }
        )
    return rows


def _plain(value):
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _archive(cik, accn, document=""):
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accn.replace('-', '')}/"
    if document:
        return base + document
    return base


def _hit_id(hit):
    source = hit.get("_source") or {}
    accession = source.get("adsh") or ""
    document = ""
    raw = hit.get("_id") or ""
    if ":" in raw:
        left, document = raw.split(":", 1)
        accession = accession or left
    return accession, document


def _first_cik(source):
    ciks = source.get("ciks") or []
    if not ciks:
        return ""
    return str(int(str(ciks[0]))).zfill(10)


def _display_name(source):
    names = source.get("display_names") or []
    return names[0] if names else None
