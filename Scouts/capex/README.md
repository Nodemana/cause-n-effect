# capex

## What it does

Records reported capex and related tagged numbers for big spenders, then searches EDGAR for capex-guidance phrases and reads recent ASX announcements. The Legate decides which of those leads pass a threshold. Tracing the spend through to tier-2 and tier-3 suppliers is later work.

## Data sources

SEC companyfacts for AMZN, ORCL, and MSFT, EDGAR full-text search for capex-guidance phrases, and the ASX announcements API. Every SEC and ASX request goes through `shared/nethttp.py`. SEC calls use `SEC_CONTACT_EMAIL` and are refused before send when it is missing.

## What it captures

Each figure lands in the shared SQLite `figures` table with the run id, quarter or period, value, currency, source link, and filing date. Cash-flow tags keep the year-to-date span SEC reported. A guidance hit stores the first amount stated in the filing description. An ASX capex headline stores the first amount in that headline.

## Filters/rules

Companyfacts keep 10-K, 10-Q, and 20-F facts (including amendments) for capex cash-flow tags and related PP&E and finance-lease tags. The latest filing wins for a period. Guidance search uses the phrases "capital expenditure", "capital expenditures", and capex, on 8-K, 10-K, and 10-Q. ASX headlines are kept when they mention capex, capital expenditure, capital investment, or property, plant.

## Constraints and limitations

SEC XBRL tagged numbers cover reported actuals only and miss untagged figures and guidance, so full-text search and release text are needed. Cash-flow tags are year-to-date, and tags change over time. The ASX feed is undocumented, returns only the latest five announcements per company (enough for a weekly run), and is health-checked. Supplier order books are not read here.

## Run

Fixture tests do not open a network connection. The live pull is for the owner to run, with `SEC_CONTACT_EMAIL` already set. It writes `/tmp/cause-n-effect-capex.sqlite` unless `--db` is passed. ASX runs only when `universe_asx.json` lists companies.

```bash
python3 Scouts/capex/tests/test_capex.py
python3 Scouts/capex/live.py
```

## Status

Structure and tests only, no live runs.
