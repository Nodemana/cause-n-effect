# debt-raise

Track new loans, credit facilities and bond issues from SEC 8-K Items 1.01/2.03, 424B prospectuses and ASX announcements.

This folder is a purpose placeholder. When the scout is built, its software lives here and does the deterministic work: fetching, parsing, storing, and diffing time-stamped figures in SQLite. Figures come only from official sources (SEC filings, ASX announcements, and company releases). SEC and ASX requests go through `shared/nethttp.py`. The Portfolio Manager decides which leads matter and whether they are priced in.
