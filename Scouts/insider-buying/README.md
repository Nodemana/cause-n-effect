# insider-buying

## What it does

Finds open-market insider purchases, including CEO buys. A CEO buy is the same row with `is_ceo` set from the officer title (`CEO` or `Chief Executive`). Clusters of different insiders at one company are flagged for the Legate.

## Data sources

SEC Form 4 and Form 4/A ownership XML, and ASX Appendix 3Y PDFs. ASX headlines come from `asx.api.markitdigital.com`. Every request goes through `shared/nethttp.py`. The SEC collector refuses to send when `SEC_CONTACT_EMAIL` is missing.

## What it captures

Insider, role, whether they are the CEO, company, ticker, shares, price, value, currency, transaction date, filing date, and source URL. History rows use figure type `insider_purchase`. Currency is USD on a Form 4 and AUD on a 3Y unless the notice says otherwise.

## Filters/rules

Open-market purchases only. Form 4 keeps transaction code `P` marked acquired. Appendix 3Y keeps on-market acquisitions. Sales, option exercises, grants, and gifts are excluded, as are off-market 3Y changes. The default minimum value is 10,000 in the transaction's own currency, with no FX conversion. A cluster is a different insider at the same company within 7 days. Missing prices, unparseable dates, joint owners, equity swaps, and amounts that disagree are flagged for the Legate and are not included.

## Constraints and limitations

Insider buying excludes sales, option exercises, grants, and gifts. Form 4 code `P` is an open-market or private purchase, and the XML does not split those. A 3Y that says neither on-market nor off-market is flagged for review. The ASX feed is undocumented, returns only the latest five announcements per company (enough for a weekly run), and is health-checked.

## Status

Structure and tests only, no live runs.
