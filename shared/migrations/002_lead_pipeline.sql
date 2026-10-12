-- Significance labels, the reject log, return-pass thesis fields, reviews,
-- rankings, and decisions. Apply with shared.pipeline_store.ensure_pipeline,
-- which records this filename in schema_migrations. The runner applies it
-- once. New tables use IF NOT EXISTS. New columns are added once.
--
-- runs, figures, rubrics, and the 001_pipeline tables stay in place.
-- Significance labels are short text plus a one-line rationale, matching
-- the pipeline architecture. Market attention is significance_attention.

PRAGMA foreign_keys=ON;

ALTER TABLE leads ADD COLUMN significance_magnitude TEXT;
ALTER TABLE leads ADD COLUMN significance_magnitude_rationale TEXT;
ALTER TABLE leads ADD COLUMN significance_novelty TEXT;
ALTER TABLE leads ADD COLUMN significance_novelty_rationale TEXT;
ALTER TABLE leads ADD COLUMN significance_attention TEXT;
ALTER TABLE leads ADD COLUMN significance_attention_rationale TEXT;
ALTER TABLE leads ADD COLUMN significance_timing TEXT;
ALTER TABLE leads ADD COLUMN significance_timing_rationale TEXT;

ALTER TABLE theses ADD COLUMN recommendation TEXT;
ALTER TABLE theses ADD COLUMN assumptions TEXT;
ALTER TABLE theses ADD COLUMN roi TEXT;
ALTER TABLE theses ADD COLUMN timeframe_days INTEGER;
ALTER TABLE theses ADD COLUMN price_run TEXT
  CHECK (price_run IS NULL OR price_run IN ('new', 'acceleration', 'priced-in'));
ALTER TABLE theses ADD COLUMN macro_context TEXT;
ALTER TABLE theses ADD COLUMN pass_number INTEGER NOT NULL DEFAULT 1
  CHECK (pass_number >= 1);

-- Official-source links the merge cites, as a JSON array. rationale stays
-- the prose evidence.
ALTER TABLE legate_merges ADD COLUMN evidence_refs TEXT;

CREATE TABLE IF NOT EXISTS lead_rejects (
  reject_id INTEGER PRIMARY KEY,
  scout TEXT NOT NULL CHECK (length(trim(scout)) > 0),
  ticker TEXT NOT NULL CHECK (length(trim(ticker)) > 0),
  cik TEXT,
  filing_date TEXT,
  source_url TEXT,
  accession TEXT,
  rejection_stage TEXT NOT NULL CHECK (rejection_stage IN ('deterministic', 'llm')),
  reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
  significance_magnitude TEXT,
  significance_magnitude_rationale TEXT,
  significance_novelty TEXT,
  significance_novelty_rationale TEXT,
  significance_attention TEXT,
  significance_attention_rationale TEXT,
  significance_timing TEXT,
  significance_timing_rationale TEXT,
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lead_reject_counterparties (
  reject_id INTEGER NOT NULL REFERENCES lead_rejects(reject_id),
  role TEXT NOT NULL CHECK (role IN ('buyer', 'supplier', 'customer')),
  name TEXT NOT NULL CHECK (length(trim(name)) > 0),
  name_normalised TEXT NOT NULL CHECK (length(trim(name_normalised)) > 0),
  ticker TEXT,
  cik TEXT,
  PRIMARY KEY (reject_id, role, name_normalised)
);

CREATE TABLE IF NOT EXISTS praetorian_reviews (
  review_id INTEGER PRIMARY KEY,
  thesis_id INTEGER NOT NULL REFERENCES theses(thesis_id),
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  assumptions_that_fail TEXT NOT NULL DEFAULT '',
  bad_sources TEXT NOT NULL DEFAULT '',
  contradicting_research TEXT NOT NULL DEFAULT '',
  other_side TEXT NOT NULL DEFAULT '',
  surviving_issues INTEGER NOT NULL CHECK (surviving_issues >= 0),
  written_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rankings (
  ranking_id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  lead_id INTEGER NOT NULL REFERENCES leads(lead_id),
  rank INTEGER NOT NULL CHECK (rank >= 1),
  roi REAL,
  timeframe_days INTEGER,
  surviving_issues INTEGER NOT NULL CHECK (surviving_issues >= 0),
  risk_count INTEGER NOT NULL CHECK (risk_count >= 0),
  rationale TEXT,
  presented INTEGER NOT NULL CHECK (presented IN (0, 1)),
  created_at TEXT NOT NULL,
  UNIQUE (run_id, lead_id),
  UNIQUE (run_id, rank)
);

CREATE TABLE IF NOT EXISTS decisions (
  decision_id INTEGER PRIMARY KEY,
  lead_id INTEGER NOT NULL REFERENCES leads(lead_id),
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('acted_on', 'not_acted_on', 'pending')),
  note TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pipeline_runs (
  run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
  token_budget INTEGER NOT NULL CHECK (token_budget >= 0),
  tokens_used INTEGER NOT NULL CHECK (tokens_used >= 0),
  provider TEXT NOT NULL CHECK (length(trim(provider)) > 0),
  started_at TEXT NOT NULL,
  finished_at TEXT
);

CREATE INDEX IF NOT EXISTS ix_lead_rejects_ticker ON lead_rejects(ticker);
CREATE INDEX IF NOT EXISTS ix_lead_rejects_cik ON lead_rejects(cik);
CREATE INDEX IF NOT EXISTS ix_lead_rejects_run ON lead_rejects(run_id);
CREATE INDEX IF NOT EXISTS ix_reject_cp_ticker ON lead_reject_counterparties(ticker);
CREATE INDEX IF NOT EXISTS ix_reject_cp_name ON lead_reject_counterparties(name_normalised);
CREATE INDEX IF NOT EXISTS ix_reviews_thesis ON praetorian_reviews(thesis_id);
CREATE INDEX IF NOT EXISTS ix_rankings_lead ON rankings(lead_id);
CREATE INDEX IF NOT EXISTS ix_decisions_lead ON decisions(lead_id);
