-- Official prices and the cleanup audit. Apply with
-- shared.pipeline_store.ensure_pipeline, which records this filename once.
--
-- prices holds an ASX Markit quote or an ingested IBKR quote. market_cap for
-- an IBKR row is SEC shares outstanding times price, in USD. Markit market_cap
-- is the header's marketCap field, in AUD. The header's currency field is the
-- company's reporting currency and is not stored as the price currency.

CREATE TABLE IF NOT EXISTS prices (
  price_id INTEGER PRIMARY KEY,
  ticker TEXT NOT NULL CHECK (length(trim(ticker)) > 0),
  exchange TEXT,
  price REAL,
  currency TEXT NOT NULL CHECK (length(trim(currency)) > 0),
  market_cap REAL,
  source TEXT NOT NULL CHECK (source IN ('asx_markit', 'ibkr')),
  status TEXT,
  fetched_at TEXT NOT NULL,
  conid TEXT,
  as_of TEXT
);

CREATE INDEX IF NOT EXISTS ix_prices_ticker ON prices(ticker, fetched_at);

CREATE TABLE IF NOT EXISTS cleanup_log (
  cleanup_id INTEGER PRIMARY KEY,
  row_id INTEGER,
  table_name TEXT NOT NULL,
  reason TEXT NOT NULL,
  run_id TEXT,
  logged_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_cleanup_log_reason ON cleanup_log(reason);
