# macro

## What it does

Pulls macroeconomic context for the second-pass Centurion. Unlike the lead scouts, it does not emit leads, does not write `lead_rejects`, and does not judge magnitude, novelty, attention, or timing.

It maintains a cached dataset. Each series stores `fetched_at` and a staleness threshold. A run fetches that series only when `fetched_at` is empty or older than the threshold. A fresh series stays as cached. The second-pass Centurion reads the cache and does not re-pull.

## Data sources

Official sources only: central banks, statistics agencies, and treasuries. That includes FRED, the BLS, the ABS, the RBA, the Federal Reserve, the US Treasury, and the official publisher of each other series (for energy, the EIA). Company figures stay on SEC filings, ASX announcements, and company releases. Media and aggregators are not a source of a macro series. A series with no official source is not cached.

When a live pull is written, those requests go through `shared/nethttp.py`, the same client SEC and ASX already use. No live pull exists yet.

## What it covers

- Interest rates and yields.
- Inflation and CPI.
- Central bank policy: QE, QT, and forward guidance.
- Credit spreads and funding conditions.
- The US dollar.
- Oil and energy prices.
- Fiscal policy and government spending.
- Supply chain disruptions.
- Geopolitics.
- Election and regulatory risk.
- Similar official series as they are added.

A numeric print stores `value`. An official statement with no single number (forward guidance, a fiscal notice, a regulatory release, an official geopolitical statement) stores `value_text` and the official URL.

## Cache

`macro_series` is one row per series. `macro_observations` is one row per series and observation date. Both are a proposed follow-up migration. They are not in `shared/migrations/001_pipeline.sql`, and nothing in the repo creates them yet. The same sketch is in [docs/pipeline-architecture.md](../../docs/pipeline-architecture.md).

`staleness_seconds` is per series. Proposed defaults, not seeded in code: one day (86400) for yields, the dollar, oil, and credit spreads; 30 days (2592000) for monthly inflation; 7 days (604800) for policy, fiscal, supply-chain, geopolitical, election, and regulatory series. When the official calendar states a release time, the series is stale at that time even if the seconds have not elapsed.

```sql
-- Follow-up migration. Not implemented.
CREATE TABLE macro_series (
  series_id TEXT PRIMARY KEY,            -- stable id, publisher plus series code
  name TEXT NOT NULL,
  category TEXT NOT NULL,                -- rates, inflation, policy, credit, fx, energy, fiscal, supply_chain, geopolitics, regulatory
  source TEXT NOT NULL,                  -- official publisher
  source_url TEXT,
  unit TEXT,
  frequency TEXT,
  staleness_seconds INTEGER NOT NULL,
  fetched_at TEXT,                       -- UTC of the last successful fetch; empty until then
  updated_at TEXT NOT NULL
);

CREATE TABLE macro_observations (
  series_id TEXT NOT NULL REFERENCES macro_series(series_id),
  obs_date TEXT NOT NULL,
  value REAL,                            -- null when the release is a statement
  value_text TEXT,                       -- official wording when there is no single number
  source_url TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  PRIMARY KEY (series_id, obs_date),
  CHECK (value IS NOT NULL OR value_text IS NOT NULL)
);

CREATE INDEX ix_macro_series_fetched_at ON macro_series(fetched_at);
```

## Constraints and limitations

Design only. No fetcher, no tests, and no live command. Geopolitics, elections, and regulatory risk are cached only from an official release, not from news. The first Centurion pass does not read this cache. The second pass does, and it does not refresh a series itself.

## Status

Documented. Not implemented.
