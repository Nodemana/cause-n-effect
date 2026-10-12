# debt-raise

Two-stage gate for use of proceeds on new loans, credit facilities, and bond issues. Sources are SEC 8-K items 1.01 and 2.03, 424B prospectus filings, and ASX debt announcements. The old `asx.com.au/asx/1/company/.../announcements` path returns 404. ASX headlines and PDFs come from the ASX research host `asx.api.markitdigital.com`, which shares the ASX throttle in `shared/nethttp.py`. `universe_asx.json` is the editable watchlist. That feed returns only the latest few headlines per code.

Stage 1 scores the filing index description (`file_description` from the EDGAR search hit), or the ASX headline, with the phrase rules in `scoring.json`. General corporate purposes scores 0. Capex, acquisitions, and a named project such as data center construction score higher. The score is the highest matching weight, so boilerplate next to a named project does not wipe the named project out. No matching phrase means the cheap filter cannot decide, and a debt headline in that state is sent to the PDF.

Stage 2 reads the primary filing text only when the cheap score is above the configurable threshold, or when stage 1 cannot decide. It uses the same rules on that text. It does not call a model. A filing the full text still cannot score is flagged `undecided` for the Portfolio Manager. Earnings-call transcripts are not a source.

This is a spotter for specific uses of proceeds, not a complete list of every borrowing.

## Analytics

Each run writes one row to the shared history database:

- `runs` — `run_id` (text, `YYYY-MM-DD-debt-raise`), `scout`, `started_at` (UTC), `notes`
- `figures` — a stated money amount from the text that was scored: ticker, figure type, value, the literal `value_text`, currency, source URL, source type, `verified` 1. The first amount is kept, so a parenthetical conversion is not substituted for it. `leads` is the shared judgment table and this scout does not write it.
- `changes` — the shared view that diffs a figure across runs
- `debt_scout_runs` — same `run_id`, the threshold used, filings seen, filings resolved by the cheap filter, filings sent to the full-document pass, and the score histogram as bucket counts (`undecided`, `0` … `4`, `5+`)
- `debt_scout_filing_scores` — accession or ASX document key, ticker, final score, cheap score, and stage reached (`cheap_filter` or `full_document`)

`migrations/001_history.sql` creates those tables. The migration is applied when a run opens the database.

A lower threshold sends more filings to the prospectus read. A higher threshold leaves mid-band scores (see `mid_min` and `mid_max` in `scoring.json`) on the cheap filter, which means those raises are never read in full.

```bash
python3 scouts/debt-raise/report.py --db history.sqlite
```

The report lists every run's threshold, cheap-filter count, full-document count, and histogram, then the mid-band scores that stayed on the cheap filter.

## Live pull

```bash
python3 scouts/debt-raise/live.py
```

The database is written under `/tmp` and is not committed. If `SEC_CONTACT_EMAIL` is unset, the script prints that and does not send an SEC request. The ASX watchlist still runs.

## Tests

```bash
python3 scouts/debt-raise/tests/test_debt_raise.py
```

The unit tests use fixtures and a fake HTTP client. They do not open a network connection. SEC and ASX requests go through `shared/nethttp.py`.
