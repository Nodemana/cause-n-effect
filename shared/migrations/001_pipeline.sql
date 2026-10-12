-- Pipeline tables: leads, rubrics, theses, critiques, and Legate merges.
-- Apply with shared.pipeline_store.ensure_pipeline, which records this file
-- in schema_migrations. Safe to run again: objects use IF NOT EXISTS, and
-- seeds insert only missing rows.
--
-- When leads has no lead_id column, ensure_pipeline renames it to
-- leads_legacy before this script, then copies those rows onto the new
-- table (rank -> priority; the old pricing status is kept in summary) and
-- drops leads_legacy.
--
-- runs, figures, scout_tests, debt_scout_runs, debt_scout_filing_scores,
-- and earlier schema_migrations rows are left in place.
--
-- Seeded threshold_value cells are configurable defaults. Rubric version 1
-- is fixed: change a threshold by inserting a new version.

PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS leads (
  lead_id INTEGER PRIMARY KEY,
  lead_type TEXT NOT NULL CHECK (lead_type IN (
    'capex', 'debt_raise', 'insider_buying', 'orders_backlog', 'accumulation'
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
  parent_position_id INTEGER REFERENCES leads(lead_id),
  priority INTEGER,
  quality_score INTEGER CHECK (quality_score IS NULL OR quality_score >= 0),
  outcome TEXT CHECK (outcome IS NULL OR length(trim(outcome)) > 0),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  CHECK (parent_position_id IS NULL OR parent_position_id <> lead_id)
);

-- Replacing a run's figures drops the junction rows. The lead and its source_url stay.
CREATE TABLE IF NOT EXISTS lead_figures (
  lead_id INTEGER NOT NULL REFERENCES leads(lead_id) ON DELETE CASCADE,
  figure_id INTEGER NOT NULL REFERENCES figures(figure_id) ON DELETE CASCADE,
  PRIMARY KEY (lead_id, figure_id)
);

CREATE TABLE IF NOT EXISTS rubrics (
  rubric_id INTEGER PRIMARY KEY,
  lead_type TEXT NOT NULL CHECK (lead_type IN (
    'capex', 'debt_raise', 'insider_buying', 'orders_backlog', 'accumulation'
  )),
  version INTEGER NOT NULL CHECK (version >= 1),
  name TEXT NOT NULL CHECK (length(trim(name)) > 0),
  fixed INTEGER NOT NULL DEFAULT 0 CHECK (fixed IN (0, 1)),
  notes TEXT,
  created_at TEXT NOT NULL,
  UNIQUE (lead_type, version)
);

CREATE TABLE IF NOT EXISTS rubric_criteria (
  criterion_id INTEGER PRIMARY KEY,
  rubric_id INTEGER NOT NULL REFERENCES rubrics(rubric_id),
  name TEXT NOT NULL CHECK (length(trim(name)) > 0),
  description TEXT NOT NULL CHECK (length(trim(description)) > 0),
  threshold_value REAL,
  threshold_unit TEXT,
  sort_order INTEGER NOT NULL CHECK (sort_order >= 1),
  configurable INTEGER NOT NULL DEFAULT 0 CHECK (configurable IN (0, 1)),
  UNIQUE (rubric_id, name),
  UNIQUE (rubric_id, sort_order),
  CHECK (
    (threshold_value IS NULL AND threshold_unit IS NULL)
    OR (
      threshold_value IS NOT NULL
      AND threshold_unit IS NOT NULL
      AND length(trim(threshold_unit)) > 0
    )
  ),
  CHECK (threshold_value IS NULL OR configurable = 1)
);

CREATE TABLE IF NOT EXISTS lead_criteria_results (
  lead_id INTEGER NOT NULL REFERENCES leads(lead_id),
  criterion_id INTEGER NOT NULL REFERENCES rubric_criteria(criterion_id),
  passed INTEGER NOT NULL CHECK (passed IN (0, 1)),
  evidence TEXT,
  source_url TEXT,
  assessed_by TEXT NOT NULL REFERENCES runs(run_id),
  assessed_at TEXT NOT NULL,
  PRIMARY KEY (lead_id, criterion_id)
);

CREATE TABLE IF NOT EXISTS theses (
  thesis_id INTEGER PRIMARY KEY,
  lead_id INTEGER NOT NULL REFERENCES leads(lead_id),
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  thesis_text TEXT NOT NULL CHECK (length(trim(thesis_text)) > 0),
  risks TEXT NOT NULL DEFAULT '',
  quality_score INTEGER NOT NULL CHECK (quality_score >= 0),
  written_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS critiques (
  critique_id INTEGER PRIMARY KEY,
  thesis_id INTEGER NOT NULL REFERENCES theses(thesis_id),
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  critique TEXT NOT NULL CHECK (length(trim(critique)) > 0),
  verdict TEXT NOT NULL CHECK (verdict IN ('revise', 'accept', 'reject')),
  round INTEGER NOT NULL CHECK (round >= 1),
  written_at TEXT NOT NULL,
  UNIQUE (thesis_id, round)
);

CREATE TABLE IF NOT EXISTS legate_merges (
  merge_id INTEGER PRIMARY KEY,
  created_at TEXT NOT NULL,
  survivor_lead_id INTEGER NOT NULL REFERENCES leads(lead_id),
  rationale TEXT,
  priority_rank INTEGER,
  bias_audit TEXT,
  audited INTEGER NOT NULL DEFAULT 0 CHECK (audited IN (0, 1)),
  run_id TEXT REFERENCES runs(run_id),
  CHECK (bias_audit IS NULL OR audited = 1)
);

CREATE TABLE IF NOT EXISTS legate_merge_leads (
  merge_id INTEGER NOT NULL REFERENCES legate_merges(merge_id),
  lead_id INTEGER NOT NULL REFERENCES leads(lead_id),
  PRIMARY KEY (merge_id, lead_id)
);

CREATE INDEX IF NOT EXISTS ix_leads_status ON leads(status);
CREATE INDEX IF NOT EXISTS ix_leads_type_ticker ON leads(lead_type, ticker);
CREATE INDEX IF NOT EXISTS ix_leads_run ON leads(run_id);
CREATE INDEX IF NOT EXISTS ix_leads_parent ON leads(parent_position_id);
CREATE INDEX IF NOT EXISTS ix_leads_scout ON leads(source_scout);
CREATE INDEX IF NOT EXISTS ix_lead_figures_figure ON lead_figures(figure_id);
CREATE INDEX IF NOT EXISTS ix_results_criterion ON lead_criteria_results(criterion_id);
CREATE INDEX IF NOT EXISTS ix_results_run ON lead_criteria_results(assessed_by);
CREATE INDEX IF NOT EXISTS ix_theses_lead ON theses(lead_id);
CREATE INDEX IF NOT EXISTS ix_theses_run ON theses(run_id);
CREATE INDEX IF NOT EXISTS ix_critiques_run ON critiques(run_id);
CREATE INDEX IF NOT EXISTS ix_merges_survivor ON legate_merges(survivor_lead_id);
CREATE INDEX IF NOT EXISTS ix_merge_leads_lead ON legate_merge_leads(lead_id);

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

CREATE TRIGGER IF NOT EXISTS trg_merge_lead_not_survivor
BEFORE INSERT ON legate_merge_leads
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'merged lead must differ from the survivor')
  WHERE NEW.lead_id = (
    SELECT survivor_lead_id FROM legate_merges WHERE merge_id = NEW.merge_id
  );
END;

CREATE TRIGGER IF NOT EXISTS trg_merge_lead_not_survivor_update
BEFORE UPDATE OF lead_id ON legate_merge_leads
FOR EACH ROW
BEGIN
  SELECT RAISE(ABORT, 'merged lead must differ from the survivor')
  WHERE NEW.lead_id = (
    SELECT survivor_lead_id FROM legate_merges WHERE merge_id = NEW.merge_id
  );
END;

INSERT INTO rubrics (lead_type, version, name, fixed, notes, created_at)
SELECT
  seed.lead_type,
  1,
  seed.name,
  0,
  'Threshold values are configurable defaults. This version is fixed; publish a new version to change a threshold.',
  '2026-01-01T00:00:00Z'
FROM (
  SELECT 'capex' AS lead_type, 'Capex' AS name
  UNION ALL SELECT 'debt_raise', 'Debt raise'
  UNION ALL SELECT 'insider_buying', 'Insider buying'
  UNION ALL SELECT 'orders_backlog', 'Orders backlog'
  UNION ALL SELECT 'accumulation', 'Accumulation'
) AS seed
WHERE NOT EXISTS (
  SELECT 1 FROM rubrics AS existing
  WHERE existing.lead_type = seed.lead_type AND existing.version = 1
);

-- Criteria are inserted while version 1 is still unfixed, then the version is fixed.
INSERT INTO rubric_criteria (
  rubric_id, name, description, threshold_value, threshold_unit, sort_order, configurable
)
SELECT rubric_id, 'min_market_cap',
  'Market cap is at least the threshold. The threshold is a configurable default.',
  2000000000, 'USD', 1, 1
FROM rubrics
WHERE lead_type = 'capex' AND version = 1
  AND NOT EXISTS (
    SELECT 1 FROM rubric_criteria
    WHERE rubric_id = rubrics.rubric_id AND name = 'min_market_cap'
  );

INSERT INTO rubric_criteria (
  rubric_id, name, description, threshold_value, threshold_unit, sort_order, configurable
)
SELECT rubric_id, 'capex_growth_yoy',
  'Capex is up at least the threshold percent versus the prior year. The threshold is a configurable default.',
  10, 'percent', 2, 1
FROM rubrics
WHERE lead_type = 'capex' AND version = 1
  AND NOT EXISTS (
    SELECT 1 FROM rubric_criteria
    WHERE rubric_id = rubrics.rubric_id AND name = 'capex_growth_yoy'
  );

INSERT INTO rubric_criteria (
  rubric_id, name, description, threshold_value, threshold_unit, sort_order, configurable
)
SELECT rubric_id, 'official_source',
  'The figure comes from a filing or a company release.',
  NULL, NULL, 3, 0
FROM rubrics
WHERE lead_type = 'capex' AND version = 1
  AND NOT EXISTS (
    SELECT 1 FROM rubric_criteria
    WHERE rubric_id = rubrics.rubric_id AND name = 'official_source'
  );

INSERT INTO rubric_criteria (
  rubric_id, name, description, threshold_value, threshold_unit, sort_order, configurable
)
SELECT rubric_id, 'min_market_cap',
  'Market cap is at least the threshold. The threshold is a configurable default.',
  500000000, 'USD', 1, 1
FROM rubrics
WHERE lead_type = 'debt_raise' AND version = 1
  AND NOT EXISTS (
    SELECT 1 FROM rubric_criteria
    WHERE rubric_id = rubrics.rubric_id AND name = 'min_market_cap'
  );

INSERT INTO rubric_criteria (
  rubric_id, name, description, threshold_value, threshold_unit, sort_order, configurable
)
SELECT rubric_id, 'raise_vs_company',
  'The stated raise is at least the threshold percent of market cap. The threshold is a configurable default.',
  5, 'percent', 2, 1
FROM rubrics
WHERE lead_type = 'debt_raise' AND version = 1
  AND NOT EXISTS (
    SELECT 1 FROM rubric_criteria
    WHERE rubric_id = rubrics.rubric_id AND name = 'raise_vs_company'
  );

INSERT INTO rubric_criteria (
  rubric_id, name, description, threshold_value, threshold_unit, sort_order, configurable
)
SELECT rubric_id, 'specific_proceeds',
  'Proceeds name spending, an acquisition, or a project, rather than general corporate purposes.',
  NULL, NULL, 3, 0
FROM rubrics
WHERE lead_type = 'debt_raise' AND version = 1
  AND NOT EXISTS (
    SELECT 1 FROM rubric_criteria
    WHERE rubric_id = rubrics.rubric_id AND name = 'specific_proceeds'
  );

INSERT INTO rubric_criteria (
  rubric_id, name, description, threshold_value, threshold_unit, sort_order, configurable
)
SELECT rubric_id, 'min_market_cap',
  'Market cap is at least the threshold. The threshold is a configurable default.',
  300000000, 'USD', 1, 1
FROM rubrics
WHERE lead_type = 'insider_buying' AND version = 1
  AND NOT EXISTS (
    SELECT 1 FROM rubric_criteria
    WHERE rubric_id = rubrics.rubric_id AND name = 'min_market_cap'
  );

INSERT INTO rubric_criteria (
  rubric_id, name, description, threshold_value, threshold_unit, sort_order, configurable
)
SELECT rubric_id, 'insider_buy_dollars',
  'Open-market purchase dollars are at least the threshold. The threshold is a configurable default.',
  10000, 'USD', 2, 1
FROM rubrics
WHERE lead_type = 'insider_buying' AND version = 1
  AND NOT EXISTS (
    SELECT 1 FROM rubric_criteria
    WHERE rubric_id = rubrics.rubric_id AND name = 'insider_buy_dollars'
  );

INSERT INTO rubric_criteria (
  rubric_id, name, description, threshold_value, threshold_unit, sort_order, configurable
)
SELECT rubric_id, 'open_market',
  'The purchase is an open-market buy, not a grant, a gift, or an option exercise.',
  NULL, NULL, 3, 0
FROM rubrics
WHERE lead_type = 'insider_buying' AND version = 1
  AND NOT EXISTS (
    SELECT 1 FROM rubric_criteria
    WHERE rubric_id = rubrics.rubric_id AND name = 'open_market'
  );

INSERT INTO rubric_criteria (
  rubric_id, name, description, threshold_value, threshold_unit, sort_order, configurable
)
SELECT rubric_id, 'min_market_cap',
  'Market cap is at least the threshold. The threshold is a configurable default.',
  1000000000, 'USD', 1, 1
FROM rubrics
WHERE lead_type = 'orders_backlog' AND version = 1
  AND NOT EXISTS (
    SELECT 1 FROM rubric_criteria
    WHERE rubric_id = rubrics.rubric_id AND name = 'min_market_cap'
  );

INSERT INTO rubric_criteria (
  rubric_id, name, description, threshold_value, threshold_unit, sort_order, configurable
)
SELECT rubric_id, 'backlog_qoq_growth',
  'Disclosed backlog grew at least the threshold percent quarter over quarter. The threshold is a configurable default.',
  10, 'percent', 2, 1
FROM rubrics
WHERE lead_type = 'orders_backlog' AND version = 1
  AND NOT EXISTS (
    SELECT 1 FROM rubric_criteria
    WHERE rubric_id = rubrics.rubric_id AND name = 'backlog_qoq_growth'
  );

INSERT INTO rubric_criteria (
  rubric_id, name, description, threshold_value, threshold_unit, sort_order, configurable
)
SELECT rubric_id, 'disclosed_demand',
  'The figure is disclosed backlog or a named order.',
  NULL, NULL, 3, 0
FROM rubrics
WHERE lead_type = 'orders_backlog' AND version = 1
  AND NOT EXISTS (
    SELECT 1 FROM rubric_criteria
    WHERE rubric_id = rubrics.rubric_id AND name = 'disclosed_demand'
  );

INSERT INTO rubric_criteria (
  rubric_id, name, description, threshold_value, threshold_unit, sort_order, configurable
)
SELECT rubric_id, 'min_market_cap',
  'Market cap is at least the threshold. The threshold is a configurable default.',
  500000000, 'USD', 1, 1
FROM rubrics
WHERE lead_type = 'accumulation' AND version = 1
  AND NOT EXISTS (
    SELECT 1 FROM rubric_criteria
    WHERE rubric_id = rubrics.rubric_id AND name = 'min_market_cap'
  );

INSERT INTO rubric_criteria (
  rubric_id, name, description, threshold_value, threshold_unit, sort_order, configurable
)
SELECT rubric_id, 'min_stake_pct',
  'The stake is at least the threshold percent. The threshold is a configurable default.',
  5, 'percent', 2, 1
FROM rubrics
WHERE lead_type = 'accumulation' AND version = 1
  AND NOT EXISTS (
    SELECT 1 FROM rubric_criteria
    WHERE rubric_id = rubrics.rubric_id AND name = 'min_stake_pct'
  );

INSERT INTO rubric_criteria (
  rubric_id, name, description, threshold_value, threshold_unit, sort_order, configurable
)
SELECT rubric_id, 'recent_window',
  'The filing date is inside the threshold number of days. The threshold is a configurable default.',
  90, 'days', 3, 1
FROM rubrics
WHERE lead_type = 'accumulation' AND version = 1
  AND NOT EXISTS (
    SELECT 1 FROM rubric_criteria
    WHERE rubric_id = rubrics.rubric_id AND name = 'recent_window'
  );

INSERT INTO rubric_criteria (
  rubric_id, name, description, threshold_value, threshold_unit, sort_order, configurable
)
SELECT rubric_id, 'not_ceasing',
  'The notice opens or changes a stake.',
  NULL, NULL, 4, 0
FROM rubrics
WHERE lead_type = 'accumulation' AND version = 1
  AND NOT EXISTS (
    SELECT 1 FROM rubric_criteria
    WHERE rubric_id = rubrics.rubric_id AND name = 'not_ceasing'
  );

UPDATE rubrics
SET fixed = 1
WHERE version = 1
  AND fixed = 0
  AND lead_type IN (
    'capex', 'debt_raise', 'insider_buying', 'orders_backlog', 'accumulation'
  );
