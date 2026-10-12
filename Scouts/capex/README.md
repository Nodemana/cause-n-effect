# capex

## What it does

Follows big spenders' capex through to tier-2 and tier-3 suppliers. The Legate decides which of those leads pass a threshold. This folder is a purpose placeholder: the scout software is not in the repo yet.

## Data sources

Official sources only: SEC filings, ASX announcements, and company releases. SEC and ASX requests will go through `shared/nethttp.py`. Blogs and media are not sources.

## What it captures

Time-stamped capex figures with source links, so a spender's outlays can be traced to the suppliers that receive them. Nothing is stored yet.

## Filters/rules

No screen is implemented yet. The intended pass is reported capex from official filings, then the suppliers those spenders buy from.

## Constraints and limitations

SEC XBRL tagged numbers cover reported actuals only and miss untagged figures and guidance, so full-text search and release text are needed. Cash-flow tags are year-to-date, and tags change over time. The ASX feed is undocumented, returns only the latest five announcements per company (enough for a weekly run), and is health-checked.

## Status

Structure only. No tests and no live runs yet.
