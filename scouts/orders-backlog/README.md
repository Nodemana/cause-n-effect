# orders-backlog

One scout, two vantage points on the same signal: disclosed demand.

Customer backlog is what a defence contractor, industrial, or chip-equipment maker says it already has. A supplier order is the same demand seen from the company that will build the turbines, transformers, or switchgear, sometimes before the customer puts it in its own backlog. This is a spotter's signal, not a full ledger. It does not reconstruct every contract, and it does not tie a supplier's press release to a customer's remaining performance obligation.

`scouts/backlog/` and `scouts/supplier-orders/` are not separate scouts. Both feeds land in one table.

## Record

Every row has the same columns: `source_side` (`customer_backlog` or `supplier_order`), `company`, `ticker`, `counterparty`, `equipment_or_period`, `value`, `currency`, `timing`, `capacity`, `source_url`, `filing_date`, `source_type`, `disclosure` (`backlog`, `order`, or `order_intake`), `flags`, and `notes`.

An empty `flags` list is the included row. A non-empty list is for the Legate. Unnamed customers on a specific order, missing values, two amounts in one sentence, a stale XBRL tag, and a tag switch are flagged rather than treated as a clean figure. Aggregate backlog and aggregate order intake have no counterparty because the filing states a total. That is not an unnamed-customer flag.

## Customer side

`universe_customers.json` is the editable universe. It is limited to `defense`, `industrial`, and `semiconductor_equipment`, including AMAT, LRCX, and KLAC. Add a name by editing that file.

Figures come from SEC company facts for `RevenueRemainingPerformanceObligation`, plus any tag named on the company. Tags in `stale_tags` (and the default stale names `BacklogOfOrders` and `Backlog`) are emitted only as review rows. If an active tag and a stale tag both appear, the stale row is also marked `tag_switch`.

Deferred revenue and contract-liability concepts are not read. Backlog is not inferred from them. Earnings-call transcripts are not parsed: a document that looks like a transcript is flagged `skipped_transcript` and no figure is taken from it.

The other customer source is 8-K exhibit text, typically the quarterly release, where a sentence states backlog and an amount.

## Supplier side

`universe_suppliers.json` is a separate watchlist. It is not the customer file. Equipment on that list is turbines, transformers, or switchgear.

Sources, all through `shared/nethttp.py`:

- SEC 8-K items 7.01, 8.01, and 9.01, exhibit text
- ASX announcements whose headlines mention an order, a contract, or that equipment
- IR or newsroom URLs listed on the supplier. The fetcher will not send those through the SEC or ASX collectors.

Order-intake sentences and intake XBRL tags (`OrdersReceived`, `OrderIntake`, or a tag named on the supplier) become `order_intake` rows for the period. A named contract becomes an `order` row with the counterparty, equipment, value, currency, delivery timing, and capacity in MW or MVA when the sentence states them.

## Tests

```bash
python3 scouts/orders-backlog/tests/test_orders_backlog.py
```

The tests use fixtures and a fake HTTP client. They do not open a network connection.
