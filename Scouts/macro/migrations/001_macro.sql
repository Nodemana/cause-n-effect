-- Macro cache for Centurions. Applied by Scouts.macro.store.migrate, which
-- records this file in schema_migrations. Safe to run again: objects use
-- IF NOT EXISTS.
--
-- runs, figures, leads, and the changes view come from shared/schema.sql.
-- This scout does not insert leads.

PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS macro_series (
    series_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    source TEXT NOT NULL,
    source_url TEXT NOT NULL,
    unit TEXT NOT NULL,
    frequency TEXT NOT NULL,
    stale_after_hours INTEGER NOT NULL CHECK (stale_after_hours > 0)
);

CREATE TABLE IF NOT EXISTS macro_observations (
    series_id TEXT NOT NULL REFERENCES macro_series(series_id),
    obs_date TEXT NOT NULL,
    value REAL NOT NULL,
    fetched_at TEXT NOT NULL,
    UNIQUE (series_id, obs_date)
);

CREATE INDEX IF NOT EXISTS idx_macro_obs_series_date
    ON macro_observations (series_id, obs_date);
