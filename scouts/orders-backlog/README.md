# orders-backlog

## What it does

Reads disclosed demand from two sides of the same signal. Customers report backlog they already hold. Suppliers announce orders for turbines, transformers, and switchgear, sometimes before that demand shows up in the customer's backlog. Both land in one table.

## Data sources

Customer figures come from SEC company facts for `RevenueRemainingPerformanceObligation` and from 8-K quarterly release text. Supplier figures come from SEC 8-K exhibit text, order-intake tags, ASX order announcements, and company release URLs on the supplier watchlist. SEC and ASX requests go through `shared/nethttp.py`.

## What it captures

One row shape for both sides: company, ticker, counterparty when named, equipment or period, value, currency, timing, capacity, source URL, and filing date. `source_side` is `customer_backlog` or `supplier_order`.

## Filters/rules

The backlog side covers defence, industrials, and chip equipment only, including AMAT, LRCX, and KLAC. Stale backlog tags are review rows, and a tag that switches from a stale name to the active tag is flagged. Unnamed customers on a specific order, missing values, and two amounts in one sentence are flagged for the Legate. Deferred revenue is not read, and transcripts are skipped.

## Constraints and limitations

SEC XBRL tagged numbers cover reported actuals only. They miss untagged figures and guidance, so full-text search and release text are needed. Supplier order books are rarely public in full, so this is a spotter's signal, not a ledger. The backlog side only covers defence, industrials, and chip equipment. The ASX feed is undocumented, returns only the latest five announcements per company (enough for a weekly run), and is health-checked.

## Status

Structure and tests only, no live runs.
