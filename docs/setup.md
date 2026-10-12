# Setup

Scouts, in each folder under `Scouts/`, do the deterministic work: fetching, parsing, storing, and diffing. The Legate applies thresholds. Centurions research leads past a threshold into theses, Praetorians attack each thesis, and the Imperator decides. Those role names are defined in the top-level README.

## Layout

```
cause-n-effect/
├── shared/          # utilities used by every scout
│   └── nethttp.py   # SEC and ASX HTTP
├── Centurions/      # research leads past the threshold into theses
├── Praetorians/     # attack each thesis
└── Scouts/
    ├── accumulation/   # 13D/13G and ASX substantial holder notices
    ├── orders-backlog/ # customer backlog and supplier orders, one table
    ├── debt-raise/     # use-of-proceeds gate for loans, facilities, and bonds
    ├── insider-buying/ # Form 4 and ASX Appendix 3Y open-market buys
    └── capex/          # big spenders' capex → 2nd/3rd-tier suppliers
```

`Scouts/capex` describes purpose only until that scout's software is added. Each other scout keeps its fetcher, parser, and tests in its own folder. Behaviour that used to sit in the top-level README (form types, filters, and what is flagged for the Legate) is in that scout's README.

## Environment

SEC requests need a contact address in `SEC_CONTACT_EMAIL`. `shared/nethttp.py` reads it from the environment at request time and puts it in the User-Agent. If the variable is missing, blank, or absent from the User-Agent, `sec_client()` raises `EndpointError` and does not send the request. ASX requests do not need the variable.

Export it in the shell before a live SEC pull. Do not commit the address. `.env` is gitignored, and so are local databases (`*.db`, `*.sqlite`, `*.sqlite3`) and `cache/`.

## HTTP

Every SEC and ASX request goes through `shared/nethttp.py`. The client spaces requests per host (0.15s for SEC, under the 10 requests/second fair-access cap; 2s for ASX, including `asx.api.markitdigital.com`), retries HTTP 403, 429, and 5xx with exponential backoff while honouring `Retry-After`, and sends an identifying User-Agent on SEC requests. See `shared/README.md` for the client API.

## Tests

Run from the repository root. These use fixtures and do not open a network connection.

```bash
python3 -m unittest discover -s Scouts/accumulation/tests -t .
python3 Scouts/orders-backlog/tests/test_orders_backlog.py
python3 Scouts/debt-raise/tests/test_debt_raise.py
python3 Scouts/insider-buying/tests/test_insider_buying.py
```

Debt-raise also has a report and a live entry point. The live script writes its database under `/tmp`. If `SEC_CONTACT_EMAIL` is unset it prints that and does not send an SEC request; the ASX watchlist still runs.

```bash
python3 Scouts/debt-raise/report.py --db history.sqlite
python3 Scouts/debt-raise/live.py
```
