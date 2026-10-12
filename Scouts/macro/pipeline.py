"""Refresh macro series that are older than their stale window. No leads."""

from datetime import date

from shared.nethttp import EndpointError
from shared.store import utc_now

from .catalog import select
from .fetcher import Guarded, MissingKey, load_observations, macro_client, read_keys
from .geopolitics import list_topics
from .store import is_stale, save_observations, upsert_series


def refresh(conn, series_ids=None, force=False, fixture_dir=None, client=None, now=None, keys=None):
    """Upsert series metadata, then fetch observations that are due.

    A series is due when --force is set or its latest fetched_at is missing
    or at least stale_after_hours old. Returns one status dict per series.
    Does not write leads, figures, or runs.
    """
    specs = select(series_ids)
    moment = now or utc_now()
    today = date.fromisoformat(moment[:10])
    supplied = read_keys() if keys is None else keys
    http = None
    if fixture_dir is None:
        secrets = [supplied.get("FRED_API_KEY"), supplied.get("EIA_API_KEY")]
        http = Guarded(client or macro_client(), secrets)
    results = []
    for spec in specs:
        with conn:
            upsert_series(conn, spec)
        if not force and not is_stale(conn, spec["series_id"], spec["stale_after_hours"], moment):
            results.append({"series_id": spec["series_id"], "status": "fresh", "count": 0})
            continue
        try:
            observations = load_observations(spec, http, supplied, fixture_dir, today)
        except MissingKey as exc:
            results.append(
                {"series_id": spec["series_id"], "status": "skipped", "reason": str(exc), "count": 0}
            )
            continue
        except EndpointError as exc:
            results.append(
                {"series_id": spec["series_id"], "status": "error", "reason": str(exc), "count": 0}
            )
            continue
        except OSError as exc:
            results.append(
                {
                    "series_id": spec["series_id"],
                    "status": "error",
                    "reason": str(exc),
                    "count": 0,
                }
            )
            continue
        if not observations:
            results.append(
                {
                    "series_id": spec["series_id"],
                    "status": "error",
                    "reason": "no observations",
                    "count": 0,
                }
            )
            continue
        with conn:
            count = save_observations(conn, spec["series_id"], observations, moment)
        results.append({"series_id": spec["series_id"], "status": "refreshed", "count": count})
    return results


def risk_note_line():
    topics = ", ".join(item["topic"] for item in list_topics())
    return f"risk notes: placeholder ({topics}); no notes loaded"
