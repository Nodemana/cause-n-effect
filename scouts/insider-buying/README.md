# insider-buying

One scout for insider and CEO buying. SEC Form 4 and Form 4/A, and ASX Appendix 3Y (change of director's interest). A CEO buy is the same row with `is_ceo` set from the officer title (`CEO` or `Chief Executive`). It is not a second scout.

Fixtures use invented names. Do not put a real person's filing in this folder.

## What is kept

Open-market purchases only.

- Form 4 transaction code `P` marked acquired (`A`). The SEC definition of `P` is an open-market or private purchase; the XML does not split those, so both stay. Sales (`S`), grants (`A`), gifts (`G`), option exercises (`M`), and the other disposition codes are dropped.
- Appendix 3Y on-market acquisitions only. Off-market changes are dropped. A notice that does not say on-market or off-market is flagged for review.

`ScreenConfig` in `filters.py` sets the minimum value (default 10,000 in the transaction's own currency, with no FX conversion) and the cluster window (default 7 days). A row is marked `cluster` when a different insider at the same company buys inside that window. The same person buying twice is not a cluster.

Missing prices, unparseable dates, joint Form 4 owners, equity swaps, and amounts that disagree are flagged for the Portfolio Manager and are not included.

## Record

`insider`, `role` (officer title, director, and/or 10% owner), `is_ceo`, `company`, `ticker`, `shares`, `price`, `value`, `currency` (USD on Form 4, AUD on a 3Y unless the notice says otherwise), `transaction_date`, `filing_date`, `source_url`, `source`, `form`, `transaction_code`, `cluster`, `flags`, `notes`.

`figures` on the same JSON document are history-db rows: `figure_type` `insider_purchase`, `instrument` `ceo` when `is_ceo` is set, and `period` of `transaction date|insider` so two buyers on one day do not collide. `verified` is 1. Review rows are not figures.

## Sources

All requests go through `shared/nethttp.py`.

- SEC full-text search for forms `4` and `4/A`, then the ownership XML named in the filing index.
- ASX Appendix 3Y headlines from `asx.api.markitdigital.com` (the company-announcements path on `asx.com.au` returns 404). PDFs are read with `pdftotext`.

`collect_sec` uses `sec_client` and refuses to send when `SEC_CONTACT_EMAIL` is missing.

## Tests

```bash
python3 scouts/insider-buying/tests/test_insider_buying.py
```

The tests use fixtures and a fake HTTP client. They do not open a network connection.
