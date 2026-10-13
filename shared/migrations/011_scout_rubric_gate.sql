-- Scout rubric gate. Leads can be filtered or waiting on a price.
-- SQLite cannot alter a CHECK, so leads is rebuilt. Foreign keys are off
-- for this script (see ensure_pipeline).
-- lead_criteria_results gains the measured value and the rubric version
-- that produced the pass or fail.

CREATE TABLE leads_gate (
  lead_id INTEGER PRIMARY KEY,
  lead_type TEXT NOT NULL CHECK (lead_type IN (
    'capex', 'debt_raise', 'insider_buying', 'orders_backlog', 'accumulation', 'macro'
  )),
  ticker TEXT NOT NULL CHECK (length(trim(ticker)) > 0),
  exchange TEXT,
  company TEXT,
  source_scout TEXT NOT NULL CHECK (length(trim(source_scout)) > 0),
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  source_url TEXT,
  summary TEXT,
  status TEXT NOT NULL DEFAULT 'fresh' CHECK (status IN (
    'fresh', 'assigned', 'researched', 'presented', 'acted', 'position',
    'closed', 'rejected', 'filtered', 'needs_price'
  )),
  parent_position_id INTEGER,
  priority INTEGER,
  quality_score INTEGER CHECK (quality_score IS NULL OR quality_score >= 0),
  outcome TEXT CHECK (outcome IS NULL OR length(trim(outcome)) > 0),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  significance_magnitude TEXT,
  significance_magnitude_rationale TEXT,
  significance_novelty TEXT,
  significance_novelty_rationale TEXT,
  significance_attention TEXT,
  significance_attention_rationale TEXT,
  significance_timing TEXT,
  significance_timing_rationale TEXT,
  CHECK (parent_position_id IS NULL OR parent_position_id <> lead_id)
);

INSERT INTO leads_gate (
  lead_id, lead_type, ticker, exchange, company, source_scout, run_id, source_url,
  summary, status, parent_position_id, priority, quality_score, outcome, created_at,
  updated_at, significance_magnitude, significance_magnitude_rationale,
  significance_novelty, significance_novelty_rationale, significance_attention,
  significance_attention_rationale, significance_timing, significance_timing_rationale
)
SELECT
  lead_id, lead_type, ticker, exchange, company, source_scout, run_id, source_url,
  summary, status, parent_position_id, priority, quality_score, outcome, created_at,
  updated_at, significance_magnitude, significance_magnitude_rationale,
  significance_novelty, significance_novelty_rationale, significance_attention,
  significance_attention_rationale, significance_timing, significance_timing_rationale
FROM leads;

DROP TABLE leads;

ALTER TABLE leads_gate RENAME TO leads;

CREATE INDEX IF NOT EXISTS ix_leads_status ON leads(status);
CREATE INDEX IF NOT EXISTS ix_leads_type_ticker ON leads(lead_type, ticker);
CREATE INDEX IF NOT EXISTS ix_leads_run ON leads(run_id);
CREATE INDEX IF NOT EXISTS ix_leads_parent ON leads(parent_position_id);
CREATE INDEX IF NOT EXISTS ix_leads_scout ON leads(source_scout);

CREATE TRIGGER IF NOT EXISTS trg_leads_parent_insert
BEFORE INSERT ON leads
FOR EACH ROW
WHEN NEW.parent_position_id IS NOT NULL
BEGIN
  SELECT RAISE(ABORT, 'parent_position_id must reference an acted or position lead')
  WHERE NOT EXISTS (
    SELECT 1 FROM leads AS parent
    WHERE parent.lead_id = NEW.parent_position_id
      AND parent.status IN ('acted', 'position')
  );
END;

CREATE TRIGGER IF NOT EXISTS trg_leads_parent_update
BEFORE UPDATE OF parent_position_id ON leads
FOR EACH ROW
WHEN NEW.parent_position_id IS NOT NULL
BEGIN
  SELECT RAISE(ABORT, 'parent_position_id must reference an acted or position lead')
  WHERE NEW.parent_position_id = NEW.lead_id
     OR NOT EXISTS (
       SELECT 1 FROM leads AS parent
       WHERE parent.lead_id = NEW.parent_position_id
         AND parent.status IN ('acted', 'position')
     );
END;

ALTER TABLE lead_criteria_results ADD COLUMN measured_value TEXT;
ALTER TABLE lead_criteria_results ADD COLUMN rubric_id INTEGER REFERENCES rubrics(rubric_id);
ALTER TABLE lead_criteria_results ADD COLUMN rubric_version INTEGER;
