-- Shared scout history, plus debt-raise run analytics.
-- runs, figures, leads, and changes are the history store capex figures use.
-- debt_scout_runs and debt_scout_filing_scores point at runs.run_id.

PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS runs(
  run_id TEXT PRIMARY KEY,
  scout TEXT NOT NULL,
  started_at TEXT NOT NULL,
  notes TEXT);

CREATE TABLE IF NOT EXISTS figures(
  figure_id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  ticker TEXT NOT NULL,
  company TEXT,
  figure_type TEXT NOT NULL,
  instrument TEXT NOT NULL DEFAULT '',
  period TEXT NOT NULL DEFAULT '',
  value REAL,
  value_text TEXT,
  currency TEXT,
  seen_at TEXT NOT NULL,
  announced_on TEXT,
  source_url TEXT,
  source_type TEXT,
  verified INTEGER NOT NULL CHECK(verified IN(0,1)),
  UNIQUE(run_id,ticker,figure_type,instrument,period));

CREATE INDEX IF NOT EXISTS ix_fig_key ON figures(ticker,figure_type,instrument,period,run_id);
CREATE INDEX IF NOT EXISTS ix_fig_run ON figures(run_id);

CREATE TABLE IF NOT EXISTS leads(
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  ticker TEXT NOT NULL,
  rank INTEGER,
  status TEXT CHECK(status IN('underappreciated','partly','priced_in','crowded')),
  rationale TEXT,
  PRIMARY KEY(run_id,ticker));

CREATE VIEW IF NOT EXISTS changes AS
SELECT * , value - prev_value AS delta,
  CASE WHEN prev_value IS NOT NULL AND prev_value<>0 THEN ROUND(100.0*(value-prev_value)/ABS(prev_value),1) END AS pct
FROM (SELECT f.ticker,f.figure_type,f.instrument,f.period,f.run_id,r.started_at,f.value_text,f.value,f.verified,
        LAG(f.value)      OVER w AS prev_value,
        LAG(f.value_text) OVER w AS prev_text,
        LAG(f.run_id)     OVER w AS prev_run
      FROM figures f JOIN runs r USING(run_id)
      WINDOW w AS (PARTITION BY f.ticker,f.figure_type,f.instrument,f.period ORDER BY r.started_at));

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
