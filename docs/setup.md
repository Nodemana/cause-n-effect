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

`Scouts/capex` pulls SEC companyfacts, EDGAR full-text capex guidance, and ASX announcements into the shared figures table. Each lead scout keeps its fetcher, parser, and tests in its own folder. `Scouts/macro` caches official series in `macro_series` and `macro_observations`. It does not write figures or leads.

## Environment

SEC requests need a contact address in `SEC_CONTACT_EMAIL`. `shared/nethttp.py` reads it from the environment at request time and puts it in the User-Agent. If the variable is missing, blank, or absent from the User-Agent, `sec_client()` raises `EndpointError` and does not send the request. ASX requests do not need the variable.

Export it in the shell before a live SEC pull. Do not commit the address. `.env` is gitignored, and so are local databases (`*.db`, `*.sqlite`, `*.sqlite3`) and `cache/`.

The macro scout reads `FRED_API_KEY` and `EIA_API_KEY` the same way: from the environment, at request time, never from the tree. If either variable is unset, that source is skipped and no request is sent. The keys are not printed.

## HTTP

Every SEC and ASX request goes through `shared/nethttp.py`. The client spaces requests per host (0.15s for SEC, under the 10 requests/second fair-access cap; 2s for ASX, including `asx.api.markitdigital.com`), retries HTTP 403, 429, and 5xx with exponential backoff while honouring `Retry-After`, and sends an identifying User-Agent on SEC requests. See `shared/README.md` for the client API.

Macro requests use the same client, with one second between calls to the same host (FRED, BLS, ABS, RBA, the NY Fed, and EIA).

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
python3 -m unittest discover -s Scouts/macro/tests -t .
```

## Lead pipeline

`python -m pipeline run` reads each scout's current figures from that scout's latest run, applies thresholds and dedupe, and writes significance task files. `python -m pipeline ingest` stores each stage's agent results and advances until the Legate selection writes one file per selected thesis. Ranking writes every candidate thesis to `data/reports/<run_id>-candidates.md` (thesis, cause to effect chain, tickers, evidence links, the full Praetorian flaw list, macro context) and queues a Legate task. The Legate selects at most 5 lead ids in `legate_selection.result.json`, with a rationale for each pick. `pipeline ingest` checks the list length and that each id is a candidate, then writes one file per selected thesis at `data/reports/<run_id>/<rank>-<ticker>.md`. Each file has the cause to effect chain, the tickers, evidence links from official sources, the macro context, the price-run classification, and then the full Praetorian critique. The Praetorian critique runs on every thesis and does not drop a lead. Ranking shows those flaws and does not remove a lead because of them. After significance, linkable capex, orders backlog, and debt raises become one capex-chain lead, and same-ticker insider and accumulation signals inside the window become one smart-money lead. A macro lead stays on its own, and its Centurion task includes related findings from those scouts (`Centurions/rules/macro_relevance.md`). A run stored with a ticker scope (debt-raise `--tickers`) replaces only those tickers; every other ticker still comes from the newest run that covers it. Earlier runs are comparison history for growth and novelty, not extra candidates. Identical figures (same ticker, type, instrument, period, and value) collapse to the newest row, and novelty compares a figure only with a different period. An insider trade more than 30 days before its filing date is dropped again at load. Significance timing uses the event date (the insider trade date, otherwise the period end, otherwise the filing date) against the sweep date, not the day the figure was stored. Novelty is first only when that ticker has no earlier figure of the same type. Timing is early at 45 days or less, on time from 46 to 120 days, and late after that. Expected return and timeframe come from the Centurion result file. Tests fill those files with an offline fixture responder that is not used by a live run. Capex growth compares any capex tag of the same span length whose period ends 365±10 days earlier; the instrument string and the period's exact formatting do not have to match. Backlog growth compares a prior quarter-end or a year-ago end, including companyfacts history stored under another instrument. A large order with no stored prior stays in the sweep, and the missing comparison is a surviving issue. A smaller order with no prior is still a reject. An ASX announcement, including a link on asx.com.au or asx.api.markitdigital.com, counts as an official source. A missing companyfacts document is logged as `no companyfacts`. `--limit` keeps the newest lead figures (`announced_on`, then `figure_id`), not alphabetical tickers. The `figures:` line is how many lead figures the sweep used after that limit. Only the latest period of each figure is rated, and capex keeps one span (quarter, then ttm). Older rows stay in `figures` for the growth comparison. A market cap that is known and below the rubric threshold is a reject. A market cap that cannot be computed is not a reject: it is the surviving issue `market cap unverified`, carried into the thesis and the ranking. Shares outstanding (`dei:EntityCommonStockSharesOutstanding`) are cached from SEC companyfacts when `SEC_CONTACT_EMAIL` is set. A companyfacts error for one ticker is logged and skipped; that ticker's cap stays unverified and the rest of the run continues. ASX prices come from the Markit company header (`priceLast` and `marketCap` in AUD; the payload `currency` field is the reporting currency). US prices are an agent handoff: `run` writes `data/agent_queue/<run_id>/prices/needed.json` and `ingest` reads `prices.result.json`. A quote is usable only when `top_status` is REALTIME or DELAYED and `as_of` is within three trading days. The US cap is SEC shares outstanding times that USD price. The sweep does not call Interactive Brokers or an unofficial price feed. A backlog or orders company total below 10% of that company's other periods is rejected as implausible. A failed hard rubric grade is still a reject. `python -m pipeline cleanup` defaults to a dry run. `--apply` copies the database, then removes duplicate figures, superseded restatements, implausible totals, 10-Q prior-year comparatives, unofficial source hosts, and orphaned leads and theses, and writes `cleanup_log`. A capex chain classifies any prior price run as new, an acceleration, or priced-in, and notes the call as a concern. A leftover cross-group merge gets a second Centurion pass that does the same. Praetorians then list every flaw, with severity and evidence, and the lead stays in ranking. `--help` does not open a database or a network connection. Macro context on the second pass is read from `macro_series` and `macro_observations` when those tables have rows. The text is the latest observation of each stored series, then the catalog series that are missing (`unavailable: FRED (no key): ...; EIA (no key): ...`). Otherwise it uses `cache/macro.json` with a `summary` string, or `--macro-cache`. If neither is present, the second pass skips macro context and the log says `macro: skipped`. When the cache is loaded the log says `macro: loaded (second-pass theses: N)`. A single lead does not read it. A capex chain does. The agent protocol, including the result schema, is in [agent-protocol.md](agent-protocol.md).

```bash
python3 -m pipeline run --help
python3 -m pipeline run --db /tmp/cause-n-effect.sqlite --dry-run
python3 -m pipeline run --db /tmp/cause-n-effect.sqlite --limit 50
python3 -m pipeline status --db /tmp/cause-n-effect.sqlite --run <run_id>
python3 -m pipeline ingest --db /tmp/cause-n-effect.sqlite --run <run_id>
python3 -m pipeline rejects --db /tmp/cause-n-effect.sqlite --ticker AMZN
python3 -m pipeline rejects --db /tmp/cause-n-effect.sqlite --counterparty "Globex Power"
python3 -m pipeline cleanup --db /tmp/cause-n-effect.sqlite
python3 -m pipeline cleanup --db /tmp/cause-n-effect.sqlite --apply
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

`--since` and `--until` bound the debt-raise SEC window. `--tickers` is a comma-separated list, filters that scout's ASX watchlist, and is stored on the run as its scope. Capex `--quarters` keeps companyfacts whose period end falls in the last N quarters (default 8). Insider buying `--limit` caps Form 4 filings (default 50). Every live command accepts `--help` and `-h`, prints usage, and exits before any request.

The macro scout does not write figures. It refreshes `macro_series` and `macro_observations` when the newest `fetched_at` is older than that series' `stale_after_hours`. `python -m pipeline run` lodges a `macro` lead from those rows when a 1-month, 3-month, or z-score move, or the Brent-WTI spread versus its prior-year range, clears the detector thresholds. The lead then uses the same agent queue as the other lead types. `--force` refreshes the selection anyway. `--series` is a comma-separated list of series ids. FRED is skipped until `FRED_API_KEY` is set, and EIA until `EIA_API_KEY` is set. `--help` exits before any request.

```bash
python3 -m Scouts.macro.live --help
python3 -m Scouts.macro.live --db /tmp/cause-n-effect.sqlite
python3 -m Scouts.macro.live --db /tmp/cause-n-effect.sqlite --fixture-dir Scouts/macro/fixtures
```

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
