-- Accumulation filers are holders, not buyers. Rebuild the counterparty
-- table so the role check accepts holder. Existing buyer, supplier, and
-- customer rows are copied.

CREATE TABLE lead_reject_counterparties_holder (
  reject_id INTEGER NOT NULL REFERENCES lead_rejects(reject_id),
  role TEXT NOT NULL CHECK (role IN ('buyer', 'supplier', 'customer', 'holder')),
  name TEXT NOT NULL CHECK (length(trim(name)) > 0),
  name_normalised TEXT NOT NULL CHECK (length(trim(name_normalised)) > 0),
  ticker TEXT,
  cik TEXT,
  PRIMARY KEY (reject_id, role, name_normalised)
);

INSERT INTO lead_reject_counterparties_holder (
  reject_id, role, name, name_normalised, ticker, cik
)
SELECT reject_id, role, name, name_normalised, ticker, cik
FROM lead_reject_counterparties;

DROP TABLE lead_reject_counterparties;

ALTER TABLE lead_reject_counterparties_holder RENAME TO lead_reject_counterparties;

CREATE INDEX IF NOT EXISTS ix_reject_cp_ticker ON lead_reject_counterparties(ticker);
CREATE INDEX IF NOT EXISTS ix_reject_cp_name ON lead_reject_counterparties(name_normalised);
