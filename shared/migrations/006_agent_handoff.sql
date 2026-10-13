-- Agent handoff. Judgement rejects are stored as agent. pipeline_runs
-- records the queue directory and which stage is waiting.

CREATE TABLE lead_rejects_agent (
  reject_id INTEGER PRIMARY KEY,
  scout TEXT NOT NULL CHECK (length(trim(scout)) > 0),
  ticker TEXT NOT NULL CHECK (length(trim(ticker)) > 0),
  cik TEXT,
  filing_date TEXT,
  source_url TEXT,
  accession TEXT,
  rejection_stage TEXT NOT NULL CHECK (rejection_stage IN ('deterministic', 'agent')),
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

INSERT INTO lead_rejects_agent (
  reject_id, scout, ticker, cik, filing_date, source_url, accession,
  rejection_stage, reason,
  significance_magnitude, significance_magnitude_rationale,
  significance_novelty, significance_novelty_rationale,
  significance_attention, significance_attention_rationale,
  significance_timing, significance_timing_rationale,
  run_id, created_at
)
SELECT
  reject_id, scout, ticker, cik, filing_date, source_url, accession,
  CASE rejection_stage WHEN 'llm' THEN 'agent' ELSE rejection_stage END,
  reason,
  significance_magnitude, significance_magnitude_rationale,
  significance_novelty, significance_novelty_rationale,
  significance_attention, significance_attention_rationale,
  significance_timing, significance_timing_rationale,
  run_id, created_at
FROM lead_rejects;

DROP TABLE lead_rejects;

ALTER TABLE lead_rejects_agent RENAME TO lead_rejects;

CREATE INDEX IF NOT EXISTS ix_lead_rejects_ticker ON lead_rejects(ticker);
CREATE INDEX IF NOT EXISTS ix_lead_rejects_cik ON lead_rejects(cik);
CREATE INDEX IF NOT EXISTS ix_lead_rejects_run ON lead_rejects(run_id);

ALTER TABLE pipeline_runs ADD COLUMN status TEXT NOT NULL DEFAULT 'complete';
ALTER TABLE pipeline_runs ADD COLUMN stage TEXT NOT NULL DEFAULT 'ranking';
ALTER TABLE pipeline_runs ADD COLUMN queue_dir TEXT;
