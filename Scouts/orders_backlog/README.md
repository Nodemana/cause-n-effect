# orders_backlog

## What it does

Reads disclosed demand from two sides of the same signal. Customers report backlog they already hold. Suppliers announce orders for turbines, transformers, and switchgear, sometimes before that demand shows up in the customer's backlog. Both land in one table.

## Data sources

Customer figures come from SEC company facts for `RevenueRemainingPerformanceObligation` and from 8-K exhibit 99 release text. Supplier figures come from SEC 8-K exhibit text, order-intake tags, ASX order announcements, and company release URLs on the supplier watchlist. ASX headlines and PDFs come from `asx.api.markitdigital.com`. SEC and ASX requests go through `shared/nethttp.py`.

## What it captures

One row shape for both sides: company, ticker, counterparty when named, equipment or period, value, currency, timing, capacity, source URL, and filing date. `source_side` is `customer_backlog` or `supplier_order`.

## Filters/rules

The backlog side covers defence, industrials, and chip equipment only, including AMAT, LRCX, and KLAC. Stale backlog tags are review rows, and a tag that switches from a stale name to the active tag is flagged. Unnamed customers on a specific order, missing values, and two amounts in one sentence are flagged for the Legate. Deferred revenue is not read, and transcripts are skipped.

## Significance

The code already reads backlog tags and order announcements. That pull is not a lead. The sector gate, the review flags, and a dedupe run first. A small LLM pass, inside a per-run token budget, then judges the shortlist and emits a lead only when the demand is genuinely interesting.

Each emitted lead stores a short label and a one-line reason on four dimensions. The same four fields are stored on a reject.

- **Magnitude.** The order or the backlog change in dollars, absolute and against prior backlog or revenue. A book that steps up hard is not a small named order.
- **Novelty.** A new customer or a new equipment class, versus a repeat frame agreement or a backlog that only rolls forward.
- **Market attention.** Whether the order or the backlog print is already noticed. Media and aggregators are for that check only. The figure stays from the filing, the ASX announcement, or the company release.
- **Timing.** How early the disclosure is versus the next earnings date, or versus the period when the backlog converts.

A candidate that fails is not a lead. It is a row in `lead_rejects`: scout `orders_backlog`, ticker, CIK when known, filing date, source URL, accession, `rejection_stage` (`deterministic` or `llm`), and the reason. A named customer on a backlog row is role `customer`. A named buyer on a supplier order is role `buyer`. Both go to `lead_reject_counterparties` with a normalised name and a ticker where it resolves. A search for buyer `AMZN` is the look-back when a supplier's stock later jumps on that spend. Adding `lead_rejects`, `lead_reject_counterparties`, and the significance columns on `leads` is a follow-up migration.

A lead that is stored goes to one Centurion, which writes one individual thesis. After the Centurions write those theses, the Legate merges linked theses. Only a merged finding goes back to one Centurion for a second, fresh full thesis pass. A lead that is not merged gets no second pass and goes straight to the Praetorians for critique. A merged finding goes to the Praetorians after that second-pass thesis.

That second pass includes a priced-in check. The Centurion looks at the actual stock price and past performance and asks whether the move already looks priced in. A big prior stock-price run does not by itself mean the thesis is old. It may be a new driver or an acceleration of an existing one. The Centurion does not reject, mark stale, or downgrade on price action alone. It records the prior run and the possible tail-end-of-cycle concern as a noted concern in the thesis, in the risks or assumptions, for the Praetorians and the Legate to weigh. That concern is that the capex or backlog may be just the tail end of an already massive run of spending or orders that has already come through. The stock price and past performance come from quote data.

## Constraints and limitations

SEC XBRL tagged numbers cover reported actuals only. They miss untagged figures and guidance, so full-text search and release text are needed. Supplier order books are rarely public in full, so this is a spotter's signal, not a ledger. The backlog side only covers defence, industrials, and chip equipment. The ASX feed is undocumented, returns only the latest five announcements per company (enough for a weekly run), and is health-checked.

## Run

The live command writes this run into the shared figures table and exits nonzero when it stores no figure. SEC companyfacts and 8-K exhibits wait until `SEC_CONTACT_EMAIL` is set. The offline check uses the AMAT companyfacts fixture.

```bash
python3 -m Scouts.orders_backlog.live --db /tmp/cause-n-effect.sqlite
python3 -m Scouts.orders_backlog.live --db /tmp/cause-n-effect.sqlite --sec-fixture Scouts/orders_backlog/fixtures/amat_facts.json --no-asx
```

## Status

Fixture tests. SEC live runs where `SEC_CONTACT_EMAIL` is set.
