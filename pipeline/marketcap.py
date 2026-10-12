"""Market cap from stored shares outstanding and an official price.

Shares come from an SEC companyfacts tag already in the figures table
(`dei:EntityCommonStockSharesOutstanding` or `CommonStockSharesOutstanding`).
The official price is a row in `prices`:

- `asx_markit`: the Markit header's marketCap, in AUD. priceLast is the
  price, also AUD. The header's currency field is not used.
- `ibkr`: a REALTIME or DELAYED quote ingested from the agent. The cap is
  shares times that USD price.

A quote older than three trading days, or any other status, is unverified.
This module does not call a price feed and it does not convert FX. A known
cap below the rubric threshold is a reject. An unverified cap is the
surviving issue "market cap unverified".
"""

from pipeline.prices import STALE_AFTER_TRADING_DAYS, latest_usable_price, trading_days_old
from shared.shares import SHARES_TYPES
from shared.store import utc_now


UNVERIFIED = "market cap unverified"


def resolve_market_cap(conn, ticker, now=None):
    """Return (cap, reason).

    `cap` is None when the official price is missing, stale, or not
    REALTIME/DELAYED, and when a US quote has no share count. A known US
    cap is shares times the IBKR price. A known ASX cap is the Markit
    marketCap in AUD.
    """
    anchor = now or utc_now()
    row = latest_usable_price(conn, ticker, anchor)
    if row is None:
        return None, UNVERIFIED
    if row["source"] == "asx_markit":
        cap = float(row["market_cap"])
        return cap, f"min_market_cap: {cap:g} AUD from ASX Markit"
    shares = latest_shares(conn, ticker)
    if shares is None or row["price"] is None:
        return None, UNVERIFIED
    cap = shares * float(row["price"])
    return cap, f"min_market_cap: {cap:g} from shares outstanding times an official price"


def latest_shares(conn, ticker):
    """Newest positive USD share count for this ticker, or None."""
    placeholders = ", ".join("?" * len(SHARES_TYPES))
    rows = conn.execute(
        f"""
        SELECT value, currency
        FROM figures
        WHERE ticker = ? AND figure_type IN ({placeholders})
          AND value IS NOT NULL AND value > 0 AND verified = 1
        ORDER BY COALESCE(announced_on, '') DESC, figure_id DESC
        """,
        (ticker, *SHARES_TYPES),
    ).fetchall()
    for row in rows:
        currency = (row["currency"] or "USD").upper()
        if currency != "USD":
            continue
        return float(row["value"])
    return None


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


def price_is_fresh(as_of, now):
    return trading_days_old(as_of, now) <= STALE_AFTER_TRADING_DAYS
