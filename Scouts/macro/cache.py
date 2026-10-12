"""Read the macro cache. Centurions call get_latest and get_series.

    from Scouts.macro.cache import get_latest, get_series, use

    use("/tmp/cause-n-effect.sqlite")
    get_latest("DFF")
    get_series("DGS10", since="2024-01-01")

`db` may be a path or an open connection. When it is omitted, the path is the
one passed to use() or the MACRO_DB environment variable.
"""

import os
import re
import sqlite3

from .store import open_db

_SINCE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_default_db = None


def use(path):
    """Remember the database get_latest and get_series open."""
    global _default_db
    _default_db = path
    return path


def get_latest(series_id, db=None):
    """The newest observation for series_id, or None when the cache has none."""
    conn, owned = _connect(db)
    try:
        row = conn.execute(
            """
            SELECT o.series_id, o.obs_date, o.value, o.fetched_at,
                   s.name, s.source, s.source_url, s.unit, s.frequency
            FROM macro_observations AS o
            JOIN macro_series AS s ON s.series_id = o.series_id
            WHERE o.series_id = ?
            ORDER BY o.obs_date DESC
            LIMIT 1
            """,
            (series_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        if owned:
            conn.close()


def get_series(series_id, since=None, db=None):
    """Observations for series_id, oldest first. `since` is YYYY-MM-DD inclusive."""
    if since is not None and not _SINCE.fullmatch(since):
        raise ValueError("since must be YYYY-MM-DD")
    conn, owned = _connect(db)
    try:
        sql = """
            SELECT series_id, obs_date, value, fetched_at
            FROM macro_observations
            WHERE series_id = ?
        """
        params = [series_id]
        if since is not None:
            sql += " AND obs_date >= ?"
            params.append(since)
        sql += " ORDER BY obs_date ASC"
        return [dict(row) for row in conn.execute(sql, params)]
    finally:
        if owned:
            conn.close()


def _connect(db):
    if isinstance(db, sqlite3.Connection):
        return db, False
    path = db
    if path is None:
        path = _default_db or os.environ.get("MACRO_DB", "").strip() or None
    if not path:
        raise RuntimeError("pass db= or call use(path) or set MACRO_DB")
    return open_db(path), True
