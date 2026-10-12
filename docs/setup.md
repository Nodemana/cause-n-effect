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
    ├── orders_backlog/ # customer backlog and supplier orders, one table
    ├── debt_raise/     # use-of-proceeds gate for loans, facilities, and bonds
    ├── insider_buying/ # Form 4 and ASX Appendix 3Y open-market buys
    ├── capex/          # big spenders' capex → 2nd/3rd-tier suppliers
    └── macro/          # cached official macro context; does not emit leads
```

`Scouts/capex` pulls SEC companyfacts, EDGAR full-text capex guidance, and ASX announcements into the shared figures table. Each lead scout keeps its fetcher, parser, and tests in its own folder. `Scouts/macro` is documented only: a cached official dataset, with no fetcher yet.

## Environment

SEC requests need a contact address in `SEC_CONTACT_EMAIL`. `shared/nethttp.py` reads it from the environment at request time and puts it in the User-Agent. If the variable is missing, blank, or absent from the User-Agent, `sec_client()` raises `EndpointError` and does not send the request. ASX requests do not need the variable.

Export it in the shell before a live SEC pull. Do not commit the address. `.env` is gitignored, and so are local databases (`*.db`, `*.sqlite`, `*.sqlite3`) and `cache/`.

## HTTP

Every SEC and ASX request goes through `shared/nethttp.py`. The client spaces requests per host (0.15s for SEC, under the 10 requests/second fair-access cap; 2s for ASX, including `asx.api.markitdigital.com`), retries HTTP 403, 429, and 5xx with exponential backoff while honouring `Retry-After`, and sends an identifying User-Agent on SEC requests. See `shared/README.md` for the client API.

## Tests

Run from the repository root. These use fixtures and do not open a network connection.

```bash
python3 -m unittest discover
python3 -m unittest discover -s shared/tests -t .
python3 -m unittest discover -s pipeline/tests -t .
python3 -m unittest discover -s Scouts/accumulation/tests -t .
python3 Scouts/orders_backlog/tests/test_orders_backlog.py
python3 Scouts/debt_raise/tests/test_debt_raise.py
python3 Scouts/insider_buying/tests/test_insider_buying.py
python3 Scouts/capex/tests/test_capex.py
```

## Lead pipeline

`python -m pipeline run` reads the latest figures each scout stored, then rates, researches, merges, critiques, ranks, and writes pending decisions. `--limit` keeps the newest lead figures (`announced_on`, then `figure_id`), not alphabetical tickers. Only the latest period of each figure is rated, and capex keeps one span (quarter, then ttm). Older rows stay in `figures` for the growth comparison. A missing market cap, a missing official price, or a failed hard rubric grade is a reject, not a silent pass. Market cap is shares outstanding times an official USD price when both are already stored. Only a merged finding gets a second Centurion pass. That pass classifies any prior price run as new, an acceleration, or priced-in, and notes the call as a concern. Praetorians then agree or disagree, with counter-evidence. `--help` does not open a database or a network connection. With no `LLM_API_KEY`, the offline stub runs every stage. Set `LLM_API_KEY` to use an OpenAI-compatible endpoint (`LLM_BASE_URL`, `LLM_MODEL`). The key is not written to the database or to a log line. Macro context on the second pass is read from `macro_series` and `macro_observations` when those tables have rows. Otherwise it uses `cache/macro.json` with a `summary` string, or `--macro-cache`. If neither is present, the second pass skips macro context. The first pass does not read it.

```bash
python3 -m pipeline run --help
python3 -m pipeline run --db /tmp/cause-n-effect.sqlite --dry-run --token-budget 80000
python3 -m pipeline run --db /tmp/cause-n-effect.sqlite --limit 50
python3 -m pipeline rejects --db /tmp/cause-n-effect.sqlite --ticker AMZN
python3 -m pipeline rejects --db /tmp/cause-n-effect.sqlite --counterparty "Globex Power"
```

## Live pulls

These write the shared `figures` table. Each run id is the UTC date, the UTC time as HHMMSS, and the scout name (`2026-10-12-143052-capex`). A second run the same day inserts its own id and leaves the first run's figures in place. Each command exits nonzero when that run stores no figure. Use one `--db` path when the runs should share a file. The file stays out of git. SEC requests run only when `SEC_CONTACT_EMAIL` is set; otherwise the command says so and does not send one. ASX still runs.

On the machine that has the contact email:

```bash
python3 -m Scouts.debt_raise.live --db /tmp/cause-n-effect.sqlite
python3 -m Scouts.debt_raise.live --db /tmp/cause-n-effect.sqlite --since 2026-10-01 --until 2026-10-08 --tickers SCG,AAPL
python3 -m Scouts.insider_buying.live --db /tmp/cause-n-effect.sqlite
python3 -m Scouts.capex.live --db /tmp/cause-n-effect.sqlite
python3 -m Scouts.capex.live --db /tmp/cause-n-effect.sqlite --quarters 8
python3 -m Scouts.orders_backlog.live --db /tmp/cause-n-effect.sqlite
python3 -m Scouts.accumulation.live --db /tmp/cause-n-effect.sqlite
```

`--since` and `--until` bound the debt-raise SEC window. `--tickers` is a comma-separated list and also filters that scout's ASX watchlist. Capex `--quarters` keeps companyfacts whose period end falls in the last N quarters (default 8). Insider buying `--limit` caps Form 4 filings (default 50). Every live command accepts `--help` and `-h`, prints usage, and exits before any request.

Offline SEC checks use a fixture instead of EDGAR and do not open a network connection:

```bash
python3 -m Scouts.debt_raise.live --db /tmp/cause-n-effect.sqlite --sec-fixture Scouts/debt_raise/fixtures/sec_check.json --no-asx
python3 -m Scouts.insider_buying.live --db /tmp/cause-n-effect.sqlite --sec-fixture Scouts/insider_buying/fixtures/sec_form4_ceo.xml --no-asx
python3 -m Scouts.capex.live --db /tmp/cause-n-effect.sqlite --sec-fixture Scouts/capex/fixtures/companyfacts_amzn.json --no-asx
python3 -m Scouts.orders_backlog.live --db /tmp/cause-n-effect.sqlite --sec-fixture Scouts/orders_backlog/fixtures/amat_facts.json --no-asx
python3 -m Scouts.accumulation.live --db /tmp/cause-n-effect.sqlite --sec-fixture Scouts/accumulation/fixtures/sec_13d.xml --no-asx
```

Debt-raise can print the threshold histogram for a database that already has a run:

```bash
python3 Scouts/debt_raise/report.py --db /tmp/cause-n-effect.sqlite
```
