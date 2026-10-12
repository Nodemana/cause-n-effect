"""Keep open-market purchases at or above a value threshold, and mark clusters."""

import sys
from datetime import date


class ScreenConfig:
    def __init__(self, min_value=10_000, cluster_window_days=7, max_trade_age_days=30):
        self.min_value = float(min_value)
        self.cluster_window_days = int(cluster_window_days)
        self.max_trade_age_days = int(max_trade_age_days)


def screen(transactions, config=None):
    """Return (included, review).

    Sales, grants, gifts, exercises, and off-market changes are omitted.
    Unparseable or ambiguous rows go to review. A cluster mark is set when
    another insider at the same company bought inside the window.
    """
    config = config or ScreenConfig()
    included = []
    review = []
    for row in transactions:
        if row["flags"]:
            review.append(row)
            continue
        if not row["open_market"]:
            continue
        if row["value"] is None:
            row["flags"].append("missing_value")
            row["notes"].append("value missing after parse")
            review.append(row)
            continue
        if row["value"] < config.min_value:
            continue
        if _stale_trade(row, config.max_trade_age_days):
            _log_stale_trade(row, config.max_trade_age_days)
            continue
        included.append(row)
    _mark_clusters(included, config.cluster_window_days)
    included.sort(key=_sort_key)
    review.sort(key=_sort_key)
    return included, review


def _mark_clusters(rows, window):
    groups = {}
    for row in rows:
        key = (row.get("ticker") or row.get("company") or "").upper()
        groups.setdefault(key, []).append(row)
    for group in groups.values():
        for row in group:
            row["cluster"] = any(
                _insider(other) != _insider(row) and _within(row, other, window)
                for other in group
            )


def _stale_trade(row, max_age_days):
    """True when the trade date is more than `max_age_days` before the filing date.

    A negative max age turns the filter off. The filing date is the clock,
    because a late or amended Form 4 can arrive long after the trade.
    """
    if max_age_days < 0:
        return False
    traded = _as_date(row.get("transaction_date"))
    filed = _as_date(row.get("filing_date"))
    if traded is None or filed is None:
        return False
    return (filed - traded).days > int(max_age_days)


def _log_stale_trade(row, max_age_days):
    print(
        "dropped stale trade"
        f" ticker={row.get('ticker') or ''}"
        f" insider={row.get('insider') or ''}"
        f" transaction_date={row.get('transaction_date') or ''}"
        f" filing_date={row.get('filing_date') or ''}"
        f" form={row.get('form') or ''}"
        f" max_age_days={int(max_age_days)}",
        file=sys.stderr,
    )


def _within(left, right, window):
    a = _as_date(left.get("transaction_date"))
    b = _as_date(right.get("transaction_date"))
    if a is None or b is None:
        return False
    return abs((a - b).days) <= window


def _as_date(value):
    try:
        year, month, day = (int(part) for part in str(value).split("-"))
        return date(year, month, day)
    except (TypeError, ValueError):
        return None


def _insider(row):
    return (row.get("insider") or "").casefold().strip()


def _sort_key(row):
    return (
        row.get("transaction_date") or "",
        row.get("ticker") or "",
        row.get("insider") or "",
    )
