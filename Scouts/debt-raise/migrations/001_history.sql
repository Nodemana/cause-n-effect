-- Debt-raise analytics. runs, figures, leads, and changes are created first
-- from shared/schema.sql (shared.store.ensure_history). These tables point at runs.run_id.

PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS debt_scout_runs (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
    started_at TEXT NOT NULL,
    threshold REAL NOT NULL,
    filings_seen INTEGER NOT NULL,
    resolved_by_cheap_filter INTEGER NOT NULL,
    sent_to_full_document INTEGER NOT NULL,
    score_histogram TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS debt_scout_filing_scores (
    score_id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    accession TEXT NOT NULL,
    ticker TEXT,
    score REAL,
    stage_reached TEXT NOT NULL CHECK (stage_reached IN ('cheap_filter', 'full_document')),
    form TEXT,
    cheap_score REAL,
    decided INTEGER NOT NULL,
    flags TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_debt_scores_run ON debt_scout_filing_scores(run_id);
