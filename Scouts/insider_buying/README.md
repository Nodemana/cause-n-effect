# insider_buying

## What it does

Finds open-market insider purchases, including CEO buys. A CEO buy is the same row with `is_ceo` set from the officer title (`CEO` or `Chief Executive`). Clusters of different insiders at one company are flagged for the Legate.

## Data sources

SEC Form 4 and Form 4/A ownership XML, and ASX Appendix 3Y PDFs. The collector skips the XSL HTML render and the full-submission text file, and reads `primary_doc.xml` (or the ownership document inside a submission wrapper, including a default namespace). ASX headlines come from `asx.api.markitdigital.com`. Every request goes through `shared/nethttp.py`. The SEC collector refuses to send when `SEC_CONTACT_EMAIL` is missing.

## What it captures

Insider, role, whether they are the CEO, company, ticker, shares, price, value, currency, transaction date, filing date, and source URL. History rows use figure type `insider_purchase`. Currency is USD on a Form 4 and AUD on a 3Y unless the notice says otherwise.

## Filters/rules

Open-market purchases only. Form 4 keeps transaction code `P` marked acquired. Appendix 3Y keeps on-market acquisitions. Sales, option exercises, grants, and gifts are excluded, as are off-market 3Y changes. The default minimum value is 10,000 in the transaction's own currency, with no FX conversion. A cluster is a different insider at the same company within 7 days. Missing prices, unparseable dates, joint owners, equity swaps, and amounts that disagree are flagged for the Legate and are not included.

## Significance

The code already keeps open-market purchases above the value minimum. That pull is not a lead. The purchase filter, the minimum value, and a dedupe run first. An external agent then judges the shortlist from a task file. The sweep does not call a model. A Centurion should not see every token purchase that cleared 10,000.

Each emitted lead stores a short label and a one-line reason on four dimensions. The same four fields are stored on a reject.

- **Magnitude.** Purchase value, absolute and against the company's size. A CEO open-market buy well above the minimum is not a token purchase.
- **Novelty.** A first open-market buy after a long gap, or a cluster of different insiders inside 7 days, versus one insider repeating a small buy.
- **Market attention.** Whether the Form 4 or Appendix 3Y is already noted. Media and aggregators are for that check only. Shares, price, and value stay from the filing.
- **Timing.** How early the transaction date sits before the next earnings date. A buy the day after earnings is late.

A candidate that fails is not a lead. It is a row in `lead_rejects`: scout `insider_buying`, ticker, CIK when known, filing date, source URL, accession, `rejection_stage` (`deterministic` or `llm`), and the reason. A buyer, supplier, or customer named in the filing is a child row in `lead_reject_counterparties` with that role, a normalised name, and a ticker where it resolves. Most Form 4s and 3Ys name none. Search is by the company ticker or by that counterparty. Those columns and `lead_rejects` / `lead_reject_counterparties` are applied by `shared/migrations/002_lead_pipeline.sql`.

A lead that is stored goes to one Centurion, which writes one individual thesis. After the Centurions write those theses, the Legate merges linked theses. Only a merged finding goes back to one Centurion for a second, fresh full thesis pass. A lead that is not merged gets no second pass and goes straight to the Praetorians for critique. A merged finding goes to the Praetorians after that second-pass thesis.

That second pass includes a priced-in check on any prior price run. The Centurion makes the call first, classifying the run as a new thesis, an acceleration of an existing thesis, or tail end / likely priced in, with its reasoning and evidence. It notes that call as a concern where relevant. There is no automatic rejection. The stock price and past performance come from quote data. The Praetorians then write every flaw in the thesis, including any objection to that classification, as flaw, severity, and evidence. The lead stays in ranking.

## Constraints and limitations

Insider buying excludes sales, option exercises, grants, and gifts. Form 4 code `P` is an open-market or private purchase, and the XML does not split those. A 3Y that says neither on-market nor off-market is flagged for review. The ASX feed is undocumented, returns only the latest five announcements per company (enough for a weekly run), and is health-checked.

## Run

The live command writes this run into the shared figures table and exits nonzero when it stores no figure. SEC Form 4 requests wait until `SEC_CONTACT_EMAIL` is set. `--limit` caps how many Form 4 filings are downloaded (default 50). The offline check uses the Form 4 fixture. `--help` prints usage and does not open a connection.

```bash
python3 -m Scouts.insider_buying.live --db /tmp/cause-n-effect.sqlite
python3 -m Scouts.insider_buying.live --db /tmp/cause-n-effect.sqlite --sec-fixture Scouts/insider_buying/fixtures/sec_form4_ceo.xml --no-asx
```

## Status

Fixture tests, plus an ASX live pull from the command above. SEC live runs where `SEC_CONTACT_EMAIL` is set.
