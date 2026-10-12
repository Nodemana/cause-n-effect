-- Named suppliers and customers pulled from SEC filings for a lead.
-- The source URL is the filing or full-text hit that named the party.
-- Apply with shared.pipeline_store.ensure_pipeline.

CREATE TABLE IF NOT EXISTS lead_counterparties (
  lead_id INTEGER NOT NULL REFERENCES leads(lead_id),
  role TEXT NOT NULL CHECK (role IN ('supplier', 'customer', 'buyer')),
  name TEXT NOT NULL CHECK (length(trim(name)) > 0),
  name_normalised TEXT NOT NULL CHECK (length(trim(name_normalised)) > 0),
  ticker TEXT,
  source_url TEXT NOT NULL CHECK (length(trim(source_url)) > 0),
  PRIMARY KEY (lead_id, role, name_normalised)
);

CREATE INDEX IF NOT EXISTS ix_lead_cp_name ON lead_counterparties(name_normalised);
CREATE INDEX IF NOT EXISTS ix_lead_cp_ticker ON lead_counterparties(ticker);
