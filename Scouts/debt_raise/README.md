# debt_raise

## What it does

Spots loans, credit facilities, and bond issues whose proceeds look like spending rather than a generic refinance. A two-stage use-of-proceeds filter scores each filing, and each run stores threshold analytics for the Legate.

## Data sources

SEC 8-K items 1.01 and 2.03, 424B prospectus filings, and ASX debt announcements. ASX headlines and PDFs come from `asx.api.markitdigital.com`, on the same throttle as other ASX calls in `shared/nethttp.py`. SEC calls need `SEC_CONTACT_EMAIL` and are refused before send when it is missing.

## What it captures

A stated money amount, with ticker, value, the literal amount text, currency, and source URL. Each filing also records its cheap score, its final score, and whether the full document was read. The run row records the threshold, how many filings the cheap filter resolved, how many went to the full read, and a score histogram.

## Filters/rules

The scout uses a two-stage use-of-proceeds filter with threshold analytics. Stage 1 scores the index description when that text is more than the form name, and otherwise the first page of the filing. An ASX headline is scored as written. The score is the highest matching weight: general corporate purposes scores 0, working capital or refinance scores 1, capex scores 3, a stated use of proceeds for an acquisition scores 4, and data center construction scores 5. Prospectus boilerplate that only mentions possible acquisitions does not match. The stated amount is the aggregate offering or principal amount; denomination minimums, par values, and registration fees are skipped. The full document is read only when that score is above the threshold (default 2) or when no phrase matches. A filing the full text still cannot score is flagged `undecided` for the Legate. Same-day filings stay distinct because the stored period is the filing date plus the accession.

## Constraints and limitations

Raising the threshold leaves mid-band scores on the cheap filter, so those raises are never read in full. The ASX feed is undocumented, returns only the latest five announcements per company (enough for a weekly run), and is health-checked. This is a spotter for specific uses of proceeds, not a list of every borrowing. Earnings-call transcripts are not a source.

## Run

The live command writes this run into the shared figures table. It exits nonzero when the run stores no figure. SEC is skipped until `SEC_CONTACT_EMAIL` is set on the machine that runs it. The offline check uses a fixture instead of EDGAR.

```bash
python3 -m Scouts.debt_raise.live --db /tmp/cause-n-effect.sqlite
python3 -m Scouts.debt_raise.live --db /tmp/cause-n-effect.sqlite --since 2026-10-01 --until 2026-10-08 --tickers SCG,AAPL --limit 50
python3 -m Scouts.debt_raise.live --db /tmp/cause-n-effect.sqlite --sec-fixture Scouts/debt_raise/fixtures/sec_check.json --no-asx
```

## Status

Fixture tests, plus an ASX live pull from the command above. SEC live runs where `SEC_CONTACT_EMAIL` is set.
