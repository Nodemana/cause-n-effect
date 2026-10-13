"""Post-signal price path.

The numbers come from stored daily closes. A missing close stays absent.
This module does not fetch a price and does not fill a gap.
"""

from datetime import date, timedelta

STALE_DAYS = 42
MOVE_PCT = 10.0
FADE_KEEP = 0.25


def window_start(signal_date, sessions=5):
    """The session date `sessions` trading days before the signal.

    Weekends are skipped. A holiday that falls on a weekday is still counted,
    because this clock has no holiday calendar.
    """
    day = _day(signal_date)
    if day is None:
        return None
    left = sessions
    cursor = day
    while left > 0:
        cursor -= timedelta(days=1)
        if cursor.weekday() < 5:
            left -= 1
    return cursor.isoformat()


def assess(signal_date, as_of, closes, current=None):
    """Days since the filing, and the path of stored closes around it.

    `closes` is a sequence of `(obs_date, close)` or `{"date", "close"}`.
    `current` is the official last price. It is used only when at least one
    daily close was stored. A last price on its own is not a path.
    """
    signal = _day(signal_date)
    sweep = _day(as_of)
    days = None
    if signal is not None and sweep is not None:
        days = (sweep - signal).days
    points = _points(closes)
    if current is not None and points and sweep is not None:
        points.append((sweep, float(current)))
    before = None
    after = []
    if signal is not None:
        start = _day(window_start(signal.isoformat()))
        for obs, price in points:
            if obs < signal and start is not None and obs >= start:
                if before is None or obs > before[0]:
                    before = (obs, price)
            elif obs >= signal and (sweep is None or obs <= sweep):
                after.append(price)
    pre = None if before is None else before[1]
    peak = max(after) if after else None
    last = after[-1] if after else None
    if current is not None and points and sweep is not None:
        last = float(current)
    runup = _pct(peak, pre)
    versus = _pct(last, pre)
    faded = None
    passed = None
    if runup is not None or versus is not None:
        popped = runup is not None and runup >= MOVE_PCT
        faded = bool(popped and versus is not None and versus <= runup * FADE_KEEP)
        passed = bool(faded or (runup is not None and runup >= MOVE_PCT) or (versus is not None and versus >= MOVE_PCT))
    return {
        "signal_date": None if signal is None else signal.isoformat(),
        "days_since_signal": days,
        "close_before_signal": pre,
        "max_runup_pct": runup,
        "current_vs_pre_pct": versus,
        "popped_and_faded": faded,
        "move_passed": passed,
        "stale": days is not None and days > STALE_DAYS,
    }


def _pct(latest, base):
    if latest is None or base is None or base <= 0 or latest <= 0:
        return None
    return (latest - base) / base * 100.0


def _points(closes):
    found = []
    for item in closes or []:
        if isinstance(item, dict):
            obs = _day(item.get("date") or item.get("obs_date"))
            raw = item.get("close")
        else:
            obs = _day(item[0])
            raw = item[1]
        try:
            price = float(raw)
        except (TypeError, ValueError):
            continue
        if obs is None or price <= 0:
            continue
        found.append((obs, price))
    found.sort(key=lambda pair: pair[0])
    return found


def _day(value):
    text = str(value or "")[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None
