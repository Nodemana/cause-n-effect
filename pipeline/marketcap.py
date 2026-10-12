"""Market cap from stored shares outstanding and an official price.

Shares come from an SEC companyfacts tag already in the figures table
(`dei:EntityCommonStockSharesOutstanding` or `CommonStockSharesOutstanding`).
The pipeline caches that share count. An official price source is TODO.
This module does not call an unofficial price feed, and it does not convert
FX. Shares without an official USD price leave the cap unknown. The caller
records that as the surviving issue "market cap unverified" instead of a
reject. A cap that is known and below the rubric threshold is still a reject.
"""

from shared.shares import SHARES_TYPES
PRICE_TYPES = ("share_price", "closing_price", "stock_price")
_OFFICIAL_TYPES = {"filing", "company_release", "asx_announcement"}
_OFFICIAL_HOSTS = ("sec.gov", "asx.com.au", "asx.api.markitdigital.com")


UNVERIFIED = "market cap unverified"


def resolve_market_cap(conn, ticker):
    """Return (usd_cap, reason).

    `usd_cap` is None when shares or an official USD price are missing.
    That reason is the surviving issue, not a reject. A known cap is
    shares times an official USD price.
    """
    shares = _latest_value(conn, ticker, SHARES_TYPES, official_only=False)
    price = _latest_value(conn, ticker, PRICE_TYPES, official_only=True)
    if shares is None or price is None:
        return None, UNVERIFIED
    cap = shares * price
    return cap, f"min_market_cap: {cap:g} from shares outstanding times an official price"


def criterion_threshold(conn, lead_type, name):
    row = conn.execute(
        """
        SELECT c.threshold_value
        FROM rubric_criteria AS c
        JOIN rubrics AS r ON r.rubric_id = c.rubric_id
        WHERE r.lead_type = ? AND r.version = 1 AND c.name = ?
        """,
        (lead_type, name),
    ).fetchone()
    if not row or row["threshold_value"] is None:
        return None
    return float(row["threshold_value"])


def _latest_value(conn, ticker, figure_types, official_only):
    placeholders = ", ".join("?" * len(figure_types))
    rows = conn.execute(
        f"""
        SELECT value, currency, source_type, source_url
        FROM figures
        WHERE ticker = ? AND figure_type IN ({placeholders})
          AND value IS NOT NULL AND value > 0 AND verified = 1
        ORDER BY COALESCE(announced_on, '') DESC, figure_id DESC
        """,
        (ticker, *figure_types),
    ).fetchall()
    for row in rows:
        currency = (row["currency"] or "USD").upper()
        if currency != "USD":
            continue
        if official_only and not _official(row["source_type"], row["source_url"]):
            continue
        return float(row["value"])
    return None


def _official(source_type, source_url):
    if (source_type or "") in _OFFICIAL_TYPES:
        return True
    url = (source_url or "").lower()
    return any(host in url for host in _OFFICIAL_HOSTS)
