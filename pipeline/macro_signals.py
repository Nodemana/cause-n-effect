"""Deterministic macro leads from stored observations.

A level lead fires when one series moves at least 10 percent in one month,
20 percent in three months, or two standard deviations versus the prior
year. A spread lead fires when Brent minus WTI is outside that pair's
prior-year range. A quiet series produces nothing.

This module reads `macro_series` and `macro_observations`. It does not
call a feed and it does not write a lead. The sweep turns each signal
into a `macro` candidate for the agent queue.
"""

import calendar
import statistics
from datetime import date, timedelta

from pipeline.load import Figure
from pipeline.prefilter import Candidate, _period_age_days

ONE_MONTH_PCT = 10.0
THREE_MONTH_PCT = 20.0
Z_SCORE = 2.0
MIN_HISTORY = 20
YEAR_DAYS = 365
MATCH_DAYS = 7
SPREADS = (("BRENT-WTI", "BRENT", "WTI", "Brent minus WTI"),)


def detect(conn, now):
    """Return signal dicts for observations on or before `now`.

    An empty list means the macro tables are absent or no series cleared
    a threshold. Each signal carries the series ids, values, dates, and
    source URLs the lead will store.
    """
    if not _tables(conn):
        return []
    as_of = _as_date(now)
    if as_of is None:
        return []
    grouped = _grouped(conn, as_of)
    signals = []
    for series_id, spec in grouped.items():
        level = _level_signal(spec, as_of)
        if level is not None:
            signals.append(level)
    for ticker, left, right, label in SPREADS:
        spread = _spread_signal(grouped.get(left), grouped.get(right), ticker, label)
        if spread is not None:
            signals.append(spread)
    signals.sort(key=lambda item: item["ticker"])
    return signals


def macro_candidates(conn, now):
    """Candidates the sweep queues beside company figures."""
    candidates = []
    for index, signal in enumerate(detect(conn, now), start=1):
        candidates.append(_candidate(signal, now, -index))
    return candidates


def _level_signal(spec, as_of):
    latest = _at_or_before(spec["points"], as_of)
    if latest is None:
        return None
    reasons = []
    changes = {}
    points = {"latest": _point(spec, latest)}
    one_month = _horizon(spec["points"], latest[0], -1)
    three_month = _horizon(spec["points"], latest[0], -3)
    if one_month is not None:
        pct = _pct(latest[1], one_month[1])
        if pct is not None:
            changes["1m_pct"] = pct
            points["1m"] = _point(spec, one_month)
            if abs(pct) >= ONE_MONTH_PCT:
                reasons.append("1m_pct")
    if three_month is not None:
        pct = _pct(latest[1], three_month[1])
        if pct is not None:
            changes["3m_pct"] = pct
            points["3m"] = _point(spec, three_month)
            if abs(pct) >= THREE_MONTH_PCT:
                reasons.append("3m_pct")
    z_score = _z_score(spec["points"], latest)
    if z_score is not None:
        changes["z_score"] = z_score
        if abs(z_score) >= Z_SCORE:
            reasons.append("z_score")
    if not reasons:
        return None
    return {
        "kind": "level",
        "ticker": spec["series_id"],
        "company": spec["name"],
        "source_url": spec["source_url"],
        "value": latest[1],
        "obs_date": latest[0].isoformat(),
        "reasons": reasons,
        "changes": changes,
        "points": points,
        "series": [points["latest"]],
    }


def _spread_signal(left, right, ticker, label):
    if left is None or right is None:
        return None
    shared = _shared(left["points"], right["points"])
    if not shared:
        return None
    latest_day, left_value, right_value = shared[-1]
    prior = [
        (day, lhs - rhs)
        for day, lhs, rhs in shared[:-1]
        if latest_day - timedelta(days=YEAR_DAYS) < day < latest_day
    ]
    if len(prior) < MIN_HISTORY:
        return None
    spread = left_value - right_value
    low = min(value for _day, value in prior)
    high = max(value for _day, value in prior)
    if low <= spread <= high:
        return None
    one_month_day = _shift_months(latest_day, -1)
    three_month_day = _shift_months(latest_day, -3)
    return {
        "kind": "spread",
        "ticker": ticker,
        "company": label,
        "source_url": left["source_url"],
        "value": spread,
        "obs_date": latest_day.isoformat(),
        "reasons": ["spread_outside_1y_range"],
        "changes": {"spread": spread, "range_low": low, "range_high": high},
        "points": {
            "latest": {
                "series_id": ticker,
                "value": spread,
                "obs_date": latest_day.isoformat(),
                "source_url": left["source_url"],
            }
        },
        "series": [
            _point(left, (latest_day, left_value)),
            _point(right, (latest_day, right_value)),
            *_leg(left, shared, one_month_day, "1m", 1),
            *_leg(right, shared, one_month_day, "1m", 2),
            *_leg(left, shared, three_month_day, "3m", 1),
            *_leg(right, shared, three_month_day, "3m", 2),
        ],
        "range_low": low,
        "range_high": high,
    }


def _candidate(signal, now, figure_id):
    text = _evidence(signal)
    figure = Figure(
        {
            "figure_id": figure_id,
            "run_id": "",
            "scout": "macro",
            "ticker": signal["ticker"],
            "company": signal["company"],
            "figure_type": "macro_" + signal["kind"],
            "instrument": signal["ticker"],
            "period": signal["obs_date"],
            "value": signal["value"],
            "value_text": text,
            "currency": "USD",
            "seen_at": str(now)[:10],
            "announced_on": signal["obs_date"],
            "source_url": signal["source_url"],
            "source_type": "official",
            "verified": 1,
        }
    )
    growth = signal["changes"].get("3m_pct", signal["changes"].get("1m_pct"))
    figure.growth_pct = growth
    figure.growth_text = text
    figure.period_age_days = _period_age_days(figure, now)
    figure.earlier_similar = False
    figure.earlier_type = False
    figure.market_cap = None
    figure.market_cap_unverified = False
    return Candidate(figure, "macro", [])


def _evidence(signal):
    parts = []
    for item in signal["series"]:
        role = item.get("role")
        prefix = f"{role} " if role else ""
        parts.append(
            f"{prefix}{item['series_id']} {item['value']:.2f} on {item['obs_date']} "
            f"source {item['source_url']}"
        )
    for name, value in signal["changes"].items():
        parts.append(f"{name} {value:.4f}")
    parts.append("reasons " + " ".join(signal["reasons"]))
    return "; ".join(parts)


def _point(spec, pair):
    day, value = pair
    return {
        "series_id": spec["series_id"],
        "value": value,
        "obs_date": day.isoformat(),
        "source_url": spec["source_url"],
    }


def _leg(spec, shared, target, role, side):
    found = None
    for row in shared:
        day = row[0]
        if day <= target and (target - day).days <= MATCH_DAYS:
            found = (day, row[side])
    if found is None:
        return []
    point = _point(spec, found)
    point["role"] = role
    return [point]


def _horizon(points, latest_day, months):
    target = _shift_months(latest_day, months)
    found = _at_or_before(points, target)
    if found is None or found[0] == latest_day:
        return None
    if (target - found[0]).days > MATCH_DAYS:
        return None
    return found


def _z_score(points, latest):
    start = latest[0] - timedelta(days=YEAR_DAYS)
    history = [value for day, value in points if start < day < latest[0]]
    if len(history) < MIN_HISTORY:
        return None
    deviation = statistics.pstdev(history)
    if deviation == 0:
        return None
    return (latest[1] - statistics.fmean(history)) / deviation


def _shared(left_points, right_points):
    right = {day: value for day, value in right_points}
    rows = []
    for day, value in left_points:
        if day in right:
            rows.append((day, value, right[day]))
    return rows


def _at_or_before(points, day):
    found = None
    for obs_date, value in points:
        if obs_date <= day:
            found = (obs_date, value)
        else:
            break
    return found


def _pct(latest, prior):
    if prior == 0:
        return None
    return (latest - prior) / abs(prior) * 100.0


def _grouped(conn, as_of):
    rows = conn.execute(
        """
        SELECT s.series_id, s.name, s.source_url, o.obs_date, o.value
        FROM macro_observations AS o
        JOIN macro_series AS s ON s.series_id = o.series_id
        WHERE o.obs_date <= ?
        ORDER BY s.series_id, o.obs_date
        """,
        (as_of.isoformat(),),
    ).fetchall()
    grouped = {}
    for row in rows:
        day = _as_date(row["obs_date"])
        if day is None:
            continue
        spec = grouped.setdefault(
            row["series_id"],
            {
                "series_id": row["series_id"],
                "name": row["name"],
                "source_url": row["source_url"],
                "points": [],
            },
        )
        spec["points"].append((day, float(row["value"])))
    return grouped


def _tables(conn):
    names = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    return "macro_series" in names and "macro_observations" in names


def _shift_months(day, months):
    index = day.month - 1 + months
    year = day.year + index // 12
    month = index % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _as_date(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
