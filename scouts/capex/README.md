# capex

Follow big spenders' capex to 2nd/3rd-tier suppliers.

This folder is a purpose placeholder. When the scout is built, its software lives here and does the deterministic work: fetching, parsing, storing, and diffing time-stamped figures in SQLite. Figures come only from official sources (SEC filings, ASX announcements, and company releases). SEC and ASX requests go through `shared/nethttp.py`. The Legate decides which leads pass a threshold.
