# capex

## What it does

Records reported capex and related tagged numbers for big spenders, then searches EDGAR for capex-guidance phrases and reads recent ASX announcements. The Legate decides which of those leads pass a threshold. Tracing the spend through to tier-2 and tier-3 suppliers is later work.

## Data sources

SEC companyfacts for AMZN, ORCL, and MSFT, EDGAR full-text search for capex-guidance phrases, and the ASX announcements API. Every SEC and ASX request goes through `shared/nethttp.py`. SEC calls use `SEC_CONTACT_EMAIL` and are refused before send when it is missing.

## What it captures

Each figure lands in the shared SQLite `figures` table with the run id, period, value, currency, source link, and filing date. The `instrument` column is `quarter`, `ytd`, or `instant`. Cash-flow tags keep the span SEC reported. When a later year-to-date span shares a start date with an earlier span and the gap is one quarter, the difference is stored as its own quarter. A guidance hit stores the first amount stated in the filing description. An ASX capex headline stores the first amount in that headline.

## Filters/rules

Companyfacts keep 10-K, 10-Q, and 20-F facts (including amendments) for capex cash-flow tags and related PP&E and finance-lease tags, and only period ends inside the last eight quarters (pass `--quarters` to change that). The latest filing wins for a period. Guidance search uses the phrases "capital expenditure", "capital expenditures", and capex, on 8-K, 10-K, and 10-Q. ASX headlines are kept when they mention capex, capital expenditure, capital investment, or property, plant.

## Significance

The pull above is the cheap filter's first half. Thresholds and a dedupe run on that pull. An external agent then judges the shortlist from a task file and emits a lead only when the print is genuinely interesting. The sweep does not call a model. A Centurion should not see every routine maintenance line.

Each emitted lead stores a short label and a one-line reason on four dimensions. The same four fields are stored on a reject.

- **Magnitude.** Absolute dollars of the print or the guidance, and that amount against the company's recent capex and size. A new $1B program is a different lead from a $10M maintenance line.
- **Novelty.** A first guide-up or a new program, versus the same run-rate repeating.
- **Market attention.** Whether the print is already being discussed. Media and aggregators are for that check only. The dollars stay from the filing, the ASX announcement, or the company release.
- **Timing.** How early the disclosure sits before the next earnings date, or before another stated capex date.

A candidate that fails is not a lead. It is a row in `lead_rejects`: scout `capex`, ticker, CIK when known, filing date, source URL, accession, `rejection_stage` (`deterministic` or `llm`), and the reason. Suppliers named in the source are child rows in `lead_reject_counterparties` with role `supplier`, a normalised name, and a ticker where it resolves. That is how a later search finds a reject that named a supplier, or finds every reject whose counterparty is a given buyer. Those columns and `lead_rejects` / `lead_reject_counterparties` are applied by `shared/migrations/002_lead_pipeline.sql`.

A lead that is stored goes to one Centurion, which writes one individual thesis. After the Centurions write those theses, the Legate merges linked theses. Only a merged finding goes back to one Centurion for a second, fresh full thesis pass. A lead that is not merged gets no second pass and goes straight to the Praetorians for critique. A merged finding goes to the Praetorians after that second-pass thesis.

That second pass includes a priced-in check on any prior price run. The Centurion makes the call first, classifying the run as a new thesis, an acceleration of an existing thesis, or tail end / likely priced in, with its reasoning and evidence. It notes that call as a concern where relevant. There is no automatic rejection. The stock price and past performance come from quote data. The Praetorians then write every flaw in the thesis, including any objection to that classification, as flaw, severity, and evidence. The lead stays in ranking.

## Constraints and limitations

SEC XBRL tagged numbers cover reported actuals only and miss untagged figures and guidance, so full-text search and release text are needed. Cash-flow tags are often year-to-date, and a single quarter is derived only when the prior span in that year is present. Tags change over time. The ASX feed is undocumented, returns only the latest five announcements per company (enough for a weekly run), and is health-checked. Supplier order books are not read here.

## Run

Fixture tests do not open a network connection. The live pull is for the owner to run, with `SEC_CONTACT_EMAIL` already set. It writes `/tmp/cause-n-effect-capex.sqlite` unless `--db` is passed. ASX runs only when `universe_asx.json` lists companies.

```bash
python3 Scouts/capex/tests/test_capex.py
python3 -m Scouts.capex.live --db /tmp/cause-n-effect.sqlite
python3 -m Scouts.capex.live --db /tmp/cause-n-effect.sqlite --quarters 8
python3 -m Scouts.capex.live --db /tmp/cause-n-effect.sqlite --sec-fixture Scouts/capex/fixtures/companyfacts_amzn.json --no-asx
```

## Status

Fixture tests, plus an ASX live pull from the command above. SEC live runs where `SEC_CONTACT_EMAIL` is set.
