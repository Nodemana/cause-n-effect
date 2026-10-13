-- Filing date and the post-signal price path. Apply with
-- shared.pipeline_store.ensure_pipeline, which records this filename once.

ALTER TABLE leads ADD COLUMN signal_date TEXT;

CREATE TABLE IF NOT EXISTS price_closes (
  close_id INTEGER PRIMARY KEY,
  ticker TEXT NOT NULL CHECK (length(trim(ticker)) > 0),
  obs_date TEXT NOT NULL,
  close REAL NOT NULL CHECK (close > 0),
  source TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  UNIQUE (ticker, obs_date)
);

CREATE INDEX IF NOT EXISTS ix_price_closes_ticker ON price_closes(ticker, obs_date);

CREATE TABLE IF NOT EXISTS lead_price_paths (
  lead_id INTEGER PRIMARY KEY REFERENCES leads(lead_id),
  signal_date TEXT,
  days_since_signal INTEGER,
  close_before_signal REAL,
  max_runup_pct REAL,
  current_vs_pre_pct REAL,
  popped_and_faded INTEGER,
  move_passed INTEGER,
  as_of TEXT,
  computed_at TEXT NOT NULL
);
