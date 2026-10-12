# cause-n-effect

Cause and effect investment agent orchestration.

## Architecture

A high-level AI agent, the Portfolio Manager, orchestrates sub-agent scouts. Each scout owns the software in its own folder under `scouts/`.

Software does the deterministic work: fetching, parsing, storing, and diffing time-stamped figures in SQLite. The agent layer handles judgment: which leads matter, and whether they are priced in.

```
cause-n-effect/
├── shared/          # utilities used by every scout
│   └── nethttp.py   # SEC and ASX HTTP
└── scouts/
    ├── accumulation/   # 13D/13G and ASX substantial holder notices
    ├── orders-backlog/ # customer backlog and supplier orders, one table
    ├── capex/          # big spenders' capex → 2nd/3rd-tier suppliers
    └── debt-raise/     # new loans, credit facilities, and bond issues
```

## Data rule

Figures come only from official sources: SEC filings, ASX announcements, and company releases.

## HTTP

Every SEC and ASX request goes through `shared/nethttp.py`. That module spaces requests per host (0.15s for SEC, under the 10 requests/second fair-access cap; 2s for ASX), retries HTTP 403, 429, and 5xx with exponential backoff while honouring `Retry-After`, and sends an identifying User-Agent on SEC requests. The SEC contact address comes from the `SEC_CONTACT_EMAIL` environment variable. The client refuses to send an SEC request when that variable is missing.

## Scouts

- `scouts/accumulation` — early M&A positioning from SEC Schedules 13D and 13G (legacy `SC 13D`/`SC 13G` and XML `SCHEDULE 13D`/`SCHEDULE 13G`, including amendments) and ASX Forms 603, 604, and 605. Software in that folder fetches, parses, and screens. Ambiguous filings are flagged for the Portfolio Manager.
- `scouts/orders-backlog` — one table for customer-side disclosed backlog (defence, industrials, chip equipment) and supplier-side orders (turbines, transformers, switchgear). A spotter's signal, not a full ledger.
- `scouts/capex` — follow big spenders' capex to 2nd/3rd-tier suppliers.
- `scouts/debt-raise` — track new loans, credit facilities and bond issues from SEC 8-K Items 1.01/2.03, 424B prospectuses and ASX announcements.

`scouts/capex` and `scouts/debt-raise` describe purpose only. Scout software is added in the scout's own folder when that scout is built.
