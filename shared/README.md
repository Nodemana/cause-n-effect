# shared

Utilities used by every scout. Each scout keeps its own fetching, parsing, and storage in its own folder. This package is the common machinery.

## nethttp

`nethttp.py` is the HTTP client for SEC and ASX. Every SEC and ASX request goes through it.

- Spacing is thread-safe and per host. `sec.gov` and its subdomains share a gate of at least 0.15 seconds, under SEC's 10 requests/second fair-access cap. `asx.com.au` and its subdomains share a gate of at least 2 seconds.
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
