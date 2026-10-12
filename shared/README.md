# shared

Utilities used by every scout. Each scout keeps its own fetching and parsing. The figures history and the HTTP client live here.

## nethttp

`nethttp.py` is the HTTP client for SEC and ASX. Every SEC and ASX request goes through it.

- Spacing is thread-safe and per host. `sec.gov` and its subdomains share a gate of at least 0.15 seconds, under SEC's 10 requests/second fair-access cap. `asx.com.au` and its subdomains share a gate of at least 2 seconds. `asx.api.markitdigital.com`, the host that serves ASX company announcements, uses that same ASX gate.
- An SEC request sends an identifying User-Agent that includes the address in the `SEC_CONTACT_EMAIL` environment variable. When that variable is missing, blank, or absent from the User-Agent, the client raises `EndpointError` and does not send the request.
- The contact address is read from the environment at request time. It is not stored in this repo.
- HTTP 403, 429, and 5xx, and network errors, retry with exponential backoff. A `Retry-After` value (delta-seconds or an HTTP date) sets the wait.

```python
from shared.nethttp import asx_client, sec_client

# SEC_CONTACT_EMAIL must already be set in the environment.
sec_client().get("https://data.sec.gov/...")
asx_client().get("https://www.asx.com.au/...")
```

`cache/` next to this module is the on-disk cache directory. It is gitignored.

## History

`schema.sql` is the shared `runs`, `figures`, `leads`, and `changes` store. `store.py` applies it. Each live run inserts a new id, the UTC date, the UTC time as HHMMSS, and the scout name (`2026-10-12-143052-capex`), plus that run's figures. A later run does not update or delete an earlier run's figures. Ids already stored, including a date and a scout name with no time, stay readable. `pipeline_store.py` applies `migrations/001_pipeline.sql` and `migrations/002_lead_pipeline.sql` on top of that schema: rubrics, theses, critiques, Legate merges, significance labels, the reject log, reviews, rankings, and decisions. It rebuilds an older `leads` table when `lead_id` is missing. Debt-raise adds its own analytics tables on top of the same `runs` row. Do not commit a database file.

Repository layout, setting `SEC_CONTACT_EMAIL`, and the scout test commands are in `docs/setup.md`.
