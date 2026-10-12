-- deal_party is an acquisition, merger, or separation counterparty.
-- Rebuild the check so that role can be stored. Existing rows are copied.

CREATE TABLE lead_counterparties_deal (
  lead_id INTEGER NOT NULL REFERENCES leads(lead_id),
  role TEXT NOT NULL CHECK (role IN ('supplier', 'customer', 'buyer', 'deal_party')),
  name TEXT NOT NULL CHECK (length(trim(name)) > 0),
  name_normalised TEXT NOT NULL CHECK (length(trim(name_normalised)) > 0),
  ticker TEXT,
  source_url TEXT NOT NULL CHECK (length(trim(source_url)) > 0),
  PRIMARY KEY (lead_id, role, name_normalised)
);

INSERT INTO lead_counterparties_deal (
  lead_id, role, name, name_normalised, ticker, source_url
)
SELECT lead_id, role, name, name_normalised, ticker, source_url
FROM lead_counterparties;

DROP TABLE lead_counterparties;

ALTER TABLE lead_counterparties_deal RENAME TO lead_counterparties;

CREATE INDEX IF NOT EXISTS ix_lead_cp_name ON lead_counterparties(name_normalised);
CREATE INDEX IF NOT EXISTS ix_lead_cp_ticker ON lead_counterparties(ticker);
