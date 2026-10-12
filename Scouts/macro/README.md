# macro

## What it does

Keeps a cached macro dataset for the second-pass Centurion. It does not emit leads, does not write `lead_rejects`, `runs`, or `figures`, and it does not judge magnitude, novelty, attention, or timing.

Centurions read the cache with `get_latest(series_id)` and `get_series(series_id, since)` in `Scouts/macro/cache.py`. The pipeline's second pass reads the same tables when they have rows, and does not re-pull. The first pass does not read the cache. Geopolitics, elections, and regulatory risk are a placeholder in `Scouts/macro/geopolitics.py`: `get_notes` returns an empty list with status `placeholder`, so a missing feed is not an all-clear. That placeholder does not fetch news and is not a numeric series.

## Data sources

Official free sources only. Requests go through `shared/nethttp.py` (`Client`, one second between calls to the same host). Company figures stay on SEC filings, ASX announcements, and company releases. Media and aggregators are not a source of a macro series.

| Series | What it is | Source |
| --- | --- | --- |
| `DFF` | Effective federal funds rate | FRED |
| `DGS2` | US Treasury 2-year yield | FRED |
| `DGS10` | US Treasury 10-year yield | FRED |
| `RBA_CASH` | RBA cash rate target (`FIRMMCRTD` on table F1) | RBA |
| `AU10Y` | Australian government 10-year yield (`FCMYGBAG10D` on table F2) | RBA |
| `US_CPI` | CPI-U all items, not seasonally adjusted (`CUUR0000SA0`) | BLS |
| `AU_CPI` | CPI all groups, eight capital cities, original, quarterly. ABS CPI 1.1.0 (17th series), key `1.10001.10.50.Q`, index 2011-12=100. Not the rebased CPI 2.0.0 index. | ABS |
| `WALCL` | Federal Reserve total assets, Wednesday level (QE/QT) | FRED |
| `RBA_ASSETS` | RBA total assets (`ARBAATAW` on table A1) | RBA |
| `IG_OAS` | ICE BofA US corporate index OAS | FRED |
| `HY_OAS` | ICE BofA US high yield index OAS | FRED |
| `USD_BROAD` | Fed trade-weighted broad dollar index | FRED |
| `AUDUSD` | USD per 1 AUD (`FXRUSD` on table F11.1) | RBA |
| `WTI` | WTI Cushing spot (`RWTC`) | EIA |
| `BRENT` | Brent Europe spot (`RBRTE`) | EIA |
| `HENRY_HUB` | Henry Hub natural gas spot (`RNGWHHD`) | EIA |
| `US_FED_OUTLAYS` | US federal outlays | FRED |
| `US_FED_SURPLUS` | US federal surplus or deficit (negative is a deficit) | FRED |
| `AU_GOV_SPENDING` | General government final consumption expenditure, current prices, seasonally adjusted | ABS |
| `AU_GFS_BALANCE` | GFS net operating balance, all levels of general government (negative is a deficit) | ABS |
| `GSCPI` | Global Supply Chain Pressure Index | NY Fed |

FRED needs `FRED_API_KEY`. EIA needs `EIA_API_KEY`. Both are read from the environment at request time. If a key is unset, that source is skipped and no request is sent. Keys are not printed, not stored, and not committed. BLS, ABS, RBA, and the NY Fed do not use a key. The public BLS window is ten calendar years. Other history starts at 2000-01-01.

BLS months and ABS quarters are stored as the last calendar day of the period. FRED, RBA, EIA, and GSCPI dates are the dates on the file.

A series is fetched again only when its newest `fetched_at` is missing or at least `stale_after_hours` old (24 hours daily, 168 weekly, 480 monthly, 1080 quarterly). `--force` refreshes the selection anyway.

## What it captures

`macro_series` holds `series_id`, `name`, `source`, `source_url`, `unit`, `frequency`, and `stale_after_hours`. `macro_observations` holds `series_id`, `obs_date`, `value`, and `fetched_at`, unique on `(series_id, obs_date)`. A later fetch updates the value and `fetched_at` for that date. The migration is `migrations/001_macro.sql`, recorded in `schema_migrations`. That name does not collide with `shared/migrations/002_lead_pipeline.sql`. Either file can be applied first.

The second pass reads `series_id`, `name`, `source`, `unit`, `obs_date`, and `value`. Those are the columns `pipeline.prompts.load_macro` selects when the tables are present.

## Constraints and limitations

This is a cache of published numbers, not a lead scout. The dollar index is the Fed's broad trade-weighted index, not the ICE dollar index. `AUDUSD` is USD per one Australian dollar, from the RBA. Australia's fiscal balance is the ABS GFS net operating balance from the latest-release workbook (the cache keeps quarters already stored). Government spending for Australia is national-accounts final consumption expenditure, in millions of AUD as ABS publishes it. The geopolitics interface does not fetch news.

## Run

`--help` exits before any request. The live command writes a gitignored SQLite file. It exits nonzero when a selected fetch fails. A missing API key is a skip, not a failure.

```bash
python3 -m Scouts.macro.live --help
python3 -m Scouts.macro.live --db /tmp/cause-n-effect.sqlite
python3 -m Scouts.macro.live --db /tmp/cause-n-effect.sqlite --series DFF,AU_CPI --force
python3 -m Scouts.macro.live --db /tmp/cause-n-effect.sqlite --fixture-dir Scouts/macro/fixtures
```

## Status

Fixture tests only. No live call runs from the tests.
