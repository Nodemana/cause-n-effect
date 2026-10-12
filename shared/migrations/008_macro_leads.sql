-- Macro leads. The lead_type and rubric checks gain 'macro'. SQLite cannot
-- alter a CHECK, so both tables are rebuilt and their rows are copied.
-- Foreign keys are off for this script (see ensure_pipeline).

CREATE TABLE leads_macro (
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
    'fresh', 'assigned', 'researched', 'presented', 'acted', 'position', 'closed', 'rejected'
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

INSERT INTO leads_macro (
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

ALTER TABLE leads_macro RENAME TO leads;

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

CREATE TABLE rubrics_macro (
  rubric_id INTEGER PRIMARY KEY,
  lead_type TEXT NOT NULL CHECK (lead_type IN (
    'capex', 'debt_raise', 'insider_buying', 'orders_backlog', 'accumulation', 'macro'
  )),
  version INTEGER NOT NULL CHECK (version >= 1),
  name TEXT NOT NULL CHECK (length(trim(name)) > 0),
  fixed INTEGER NOT NULL DEFAULT 0 CHECK (fixed IN (0, 1)),
  notes TEXT,
  created_at TEXT NOT NULL,
  UNIQUE (lead_type, version)
);

INSERT INTO rubrics_macro (rubric_id, lead_type, version, name, fixed, notes, created_at)
SELECT rubric_id, lead_type, version, name, fixed, notes, created_at
FROM rubrics;

DROP TRIGGER IF EXISTS trg_rubric_criteria_fixed_no_insert;
DROP TRIGGER IF EXISTS trg_rubric_criteria_fixed_no_update;
DROP TRIGGER IF EXISTS trg_rubric_criteria_fixed_no_delete;

DROP TABLE rubrics;

ALTER TABLE rubrics_macro RENAME TO rubrics;

CREATE TRIGGER IF NOT EXISTS trg_rubrics_fixed_no_update
BEFORE UPDATE ON rubrics
FOR EACH ROW
WHEN OLD.fixed = 1
BEGIN
  SELECT RAISE(ABORT, 'rubric version is fixed');
END;

CREATE TRIGGER IF NOT EXISTS trg_rubrics_fixed_no_delete
BEFORE DELETE ON rubrics
FOR EACH ROW
WHEN OLD.fixed = 1
BEGIN
  SELECT RAISE(ABORT, 'rubric version is fixed');
END;

CREATE TRIGGER IF NOT EXISTS trg_rubric_criteria_fixed_no_insert
BEFORE INSERT ON rubric_criteria
FOR EACH ROW
WHEN (SELECT fixed FROM rubrics WHERE rubric_id = NEW.rubric_id) = 1
BEGIN
  SELECT RAISE(ABORT, 'rubric version is fixed');
END;

CREATE TRIGGER IF NOT EXISTS trg_rubric_criteria_fixed_no_update
BEFORE UPDATE ON rubric_criteria
FOR EACH ROW
WHEN (SELECT fixed FROM rubrics WHERE rubric_id = OLD.rubric_id) = 1
BEGIN
  SELECT RAISE(ABORT, 'rubric version is fixed');
END;

CREATE TRIGGER IF NOT EXISTS trg_rubric_criteria_fixed_no_delete
BEFORE DELETE ON rubric_criteria
FOR EACH ROW
WHEN (SELECT fixed FROM rubrics WHERE rubric_id = OLD.rubric_id) = 1
BEGIN
  SELECT RAISE(ABORT, 'rubric version is fixed');
END;

INSERT INTO rubrics (lead_type, version, name, fixed, notes, created_at)
SELECT
  'macro',
  1,
  'Macro',
  0,
  'A level move or a Brent-WTI spread outside its prior-year range. Thresholds are applied by the detector before a lead is queued.',
  '2026-01-01T00:00:00Z'
WHERE NOT EXISTS (
  SELECT 1 FROM rubrics AS existing
  WHERE existing.lead_type = 'macro' AND existing.version = 1
);

INSERT INTO rubric_criteria (
  rubric_id, name, description, threshold_value, threshold_unit, sort_order, configurable
)
SELECT rubric_id, 'signal_threshold',
  'The stored 1-month move, 3-month move, z-score, or Brent-WTI spread versus its prior-year range clears the detector threshold.',
  NULL, NULL, 1, 0
FROM rubrics
WHERE lead_type = 'macro' AND version = 1
  AND NOT EXISTS (
    SELECT 1 FROM rubric_criteria
    WHERE rubric_id = rubrics.rubric_id AND name = 'signal_threshold'
  );

INSERT INTO rubric_criteria (
  rubric_id, name, description, threshold_value, threshold_unit, sort_order, configurable
)
SELECT rubric_id, 'cause_to_effects',
  'The thesis maps the macro cause to the likely effects.',
  NULL, NULL, 2, 0
FROM rubrics
WHERE lead_type = 'macro' AND version = 1
  AND NOT EXISTS (
    SELECT 1 FROM rubric_criteria
    WHERE rubric_id = rubrics.rubric_id AND name = 'cause_to_effects'
  );

INSERT INTO rubric_criteria (
  rubric_id, name, description, threshold_value, threshold_unit, sort_order, configurable
)
SELECT rubric_id, 'winners_and_losers',
  'The thesis names winning tickers and losing tickers.',
  NULL, NULL, 3, 0
FROM rubrics
WHERE lead_type = 'macro' AND version = 1
  AND NOT EXISTS (
    SELECT 1 FROM rubric_criteria
    WHERE rubric_id = rubrics.rubric_id AND name = 'winners_and_losers'
  );

UPDATE rubrics
SET fixed = 1
WHERE lead_type = 'macro' AND version = 1 AND fixed = 0;
