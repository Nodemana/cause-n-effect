# accumulation

## What it does

Finds early stake building in official ownership filings. The scout fetches, parses, and screens. Pattern breaks are flagged for the Legate instead of being treated as clean leads.

## Data sources

SEC Schedules 13D and 13G, including amendments, in both the legacy `SC 13D` / `SC 13G` types and the XML `SCHEDULE 13D` / `SCHEDULE 13G` types. ASX substantial holder notices are Form 603 (new), Form 604 (change), and Form 605 (ceasing), from `asx.api.markitdigital.com`. Every SEC and ASX request goes through `shared/nethttp.py`. The SEC collector refuses to send when `SEC_CONTACT_EMAIL` is missing.

## What it captures

Each position records the filer, the target, the stake percent, the share count when the form states one, the event date, and the filing date. A 13D also keeps the Item 4 purpose text. A joint filing stays one position so the same shares are not counted twice.

## Filters/rules

A filing is a candidate only inside a recent window (default 90 days), only when it is not a ceasing notice, and only when at least one screen matches: a stake at or above the threshold (default 5%), a repeat filer across more than one target, or sector concentration by the same filer. Unparseable stakes and dates, conflicting figures, a 13G-to-13D switch, and control-purpose language are pattern breaks and go to review.

## Significance

The code already fetches and screens stakes. That pull is not a lead. The recent-window, ceasing, and stake screens, plus a dedupe, run first. An external agent then judges the shortlist from a task file and emits a lead only when the stake is genuinely interesting. The sweep does not call a model.

Each emitted lead stores a short label and a one-line reason on four dimensions. The same four fields are stored on a reject.

- **Magnitude.** Stake percent, and dollars when the form states a share count, against the company's size. A first stake at the screen threshold is not a routine amendment of a few basis points, and a control-sized block is not a toehold.
- **Novelty.** A first filing by this filer, a 13G-to-13D switch, or control-purpose language, versus another amendment of a stake already on file.
- **Market attention.** Whether the stake is already written up. Media and aggregators are for that check only. The percent and the share count stay from the 13D, the 13G, or the ASX notice.
- **Timing.** How soon the filing follows the event date, and how far it sits before the next earnings date or other catalyst.

A candidate that fails is not a lead. It is a row in `lead_rejects`: scout `accumulation`, ticker of the target, CIK when known, filing date, source URL, accession, `rejection_stage` (`deterministic` or `llm`), and the reason. If Item 4 names a buyer, supplier, or customer, that party is a child row in `lead_reject_counterparties` with the matching role, a normalised name, and a ticker where it resolves. Search is by the target ticker or by that counterparty. Those columns and `lead_rejects` / `lead_reject_counterparties` are applied by `shared/migrations/002_lead_pipeline.sql`.

A lead that is stored goes to one Centurion, which writes one individual thesis. After the Centurions write those theses, the Legate merges linked theses. Only a merged finding goes back to one Centurion for a second, fresh full thesis pass. A lead that is not merged gets no second pass and goes straight to the Praetorians for critique. A merged finding goes to the Praetorians after that second-pass thesis.

That second pass includes a priced-in check on any prior price run. The Centurion makes the call first, classifying the run as a new thesis, an acceleration of an existing thesis, or tail end / likely priced in, with its reasoning and evidence. It notes that call as a concern where relevant. There is no automatic rejection. The stock price and past performance come from quote data. The Praetorians then review that classification and state whether they agree or disagree, with counter-evidence.

## Constraints and limitations

The ASX announcements feed is undocumented, returns only the latest five announcements per company (enough for a weekly run), and is health-checked. Sector is not on the filing; the screen uses a map supplied by the caller. The Legate decides which flagged leads pass a threshold.

## Run

The live command writes screened stakes into the shared figures table and exits nonzero when it stores no figure. A figure is `verified` when the primary document's cover page yielded both the percent of class and the share count. Currency is USD for an SEC stake and AUD for an ASX stake. The source URL is that primary document, and the accession is stored with it. Warrant, unit, and OTC-only tickers (such as a `NUAIW` warrant line or an `FMTOF` OTC foreign line) are skipped unless `universe_sec.json` lists them; a common-stock ticker for the same CIK is preferred. SEC 13D/13G requests wait until `SEC_CONTACT_EMAIL` is set. The offline check uses the Schedule 13D fixture. `--help` prints usage and does not open a connection.

```bash
python3 -m Scouts.accumulation.live --db /tmp/cause-n-effect.sqlite
python3 -m Scouts.accumulation.live --db /tmp/cause-n-effect.sqlite --sec-fixture Scouts/accumulation/fixtures/sec_13d.xml --no-asx
```

## Status

Fixture tests. SEC live runs where `SEC_CONTACT_EMAIL` is set.
