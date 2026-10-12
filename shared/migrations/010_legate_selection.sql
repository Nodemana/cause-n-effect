-- The Legate's shortlist. Ranking stores every candidate. This table stores the picks.
CREATE TABLE IF NOT EXISTS legate_selections (
  selection_id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  lead_id INTEGER NOT NULL REFERENCES leads(lead_id),
  pick_order INTEGER NOT NULL CHECK (pick_order >= 1),
  rationale TEXT NOT NULL CHECK (length(trim(rationale)) > 0),
  created_at TEXT NOT NULL,
  UNIQUE (run_id, lead_id),
  UNIQUE (run_id, pick_order)
);

CREATE INDEX IF NOT EXISTS ix_legate_selections_run ON legate_selections(run_id);
