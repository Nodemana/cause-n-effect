# Agent protocol

The live sweep does not call a model and does not read an API key. `python -m pipeline run` does the deterministic work, writes one task file per judgement, records `awaiting_agents`, and exits 0. An external agent reads each task and writes a result file. `python -m pipeline ingest` checks those files and advances.

Tests drive the same queue with `pipeline.fixture.run_with_fixture`. That responder is not imported by the live commands.

## Commands

```bash
python3 -m pipeline run --db /tmp/cause-n-effect.sqlite
python3 -m pipeline status --db /tmp/cause-n-effect.sqlite --run <run_id>
python3 -m pipeline ingest --db /tmp/cause-n-effect.sqlite --run <run_id>
```

Repeat `status` and `ingest` after each stage. The last successful `ingest` prints `status: complete`, `stage: legate_selection`, and the ranking and decision counts.

`--queue-dir` overrides the default `data/agent_queue`. `ingest` must use the same directory the run stored.

`--dry-run` prints how many significance tasks would be queued. It writes no queue files, no rejects, and no `pipeline_runs` row.

When `SEC_CONTACT_EMAIL` is set, `run` caches shares outstanding and `ingest` names counterparties after significance. The address is the SEC User-Agent and is not logged. A source error drops that company's parties. With the variable unset, neither step opens a socket.

## Prices

Before significance, each US ticker that does not already have a fresh official price is listed in:

```text
data/agent_queue/<pipeline_run_id>/prices/needed.json
```

The file has `run_id`, `as_of` (the sweep clock), `tickers` (`ticker`, `exchange`), and `history`. `history` names the optional closes the report can show: `prior_year_close` (last close of the prior calendar year), `close_1y` (close 52 weeks before `as_of`), `low_52w`, and `high_52w`. Those closes come from historical bars. The pipeline does not call Interactive Brokers and does not calculate a close that was not supplied.

Write one JSON list, or one JSON object when there is a single ticker, to:

```text
data/agent_queue/<pipeline_run_id>/prices/prices.result.json
```

Each object requires `{ticker, conid, last, top_status, as_of}`. It may also include `prior_year_close`, `close_1y`, `low_52w`, and `high_52w`, each a positive number or null. Extra keys are rejected. `top_status` of `REALTIME` or `DELAYED`, a positive `last`, and an `as_of` within three trading days of the sweep clock are stored. Any other status, a missing price, or an older quote leaves that ticker unverified and does not block the other tickers. A missing history field does not reject the quote. The thesis file shows `n/a` for that cell. `ingest` then sets the US market cap to SEC shares outstanding times `last`, in USD.

ASX tickers are not in that file. `run` requests `https://asx.api.markitdigital.com/asx-research/1.0/companies/{code}/header` with a browser User-Agent. `priceLast` and `marketCap` are AUD. The payload's `currency` field is the company's reporting currency, not the price currency. A failed header leaves that ticker unverified and the sweep continues.

A missing or malformed `prices.result.json` does not advance the stage.

## Stages

0. **prices** — one `needed.json` for US tickers that need a quote. Skipped when every US ticker already has a fresh quote.
1. **significance** — one task per figure that passed thresholds and dedupe, and one task per macro signal.
2. **centurion_1** — one task per lead after the deterministic combine. A capex chain (capex, orders backlog, and debt where they link) is one lead. Smart money (insider and accumulation on the same ticker inside the window) is one lead. Macro stays one lead, and its task lists related findings from the other two. An unlinked figure stays a single lead.
3. **Legate merge** — deterministic, no task file. Chain types are not merged again. A leftover cross-group link, such as capex with insider buying, becomes one merged lead.
4. **centurion_2** — one task per leftover merged lead. A sweep with no leftover merge skips this stage.
5. **praetorian** — one task per lead that still has a thesis (the chain or smart-money lead, a single lead, or a leftover merge). The result is a list of flaws. Each flaw has `flaw`, `severity` (`low`, `moderate`, or `high`), and `evidence`. An empty list is valid. The critique does not reject, drop, or filter a lead.
6. **ranking** — deterministic. Score is expected return, plus growth and magnitude, minus 8 points per deterministic surviving issue (a significance soft issue, an unverified market cap, or an open backlog growth question). Praetorian flaws are not part of that count and do not remove a lead. Every ranked thesis is written to `data/reports/<run_id>-candidates.md`, with its full flaw list, and one `legate_selection` task is queued.
7. **legate_selection** — the Legate writes `legate_selection.result.json`. `lead_ids` has at most 5 integers, each a candidate from that task. `rationale` has one non-empty string per chosen id, keyed by the id as text. `ingest` rejects a longer list, a duplicate, an unknown id, or a missing rationale, and leaves the stage where it is. A valid file writes one markdown file per selected thesis at `data/reports/<run_id>/<rank>-<ticker>.md`. The rank is the selection order. Under the title, the file has a price table for every ticker, then a bottom line (recommendation, expected return, timeframe, conviction, biggest risk), then cause to effect, the thesis, evidence links from official sources, risks, assumptions, the Praetorian critique, the Legate rationale, and the macro series that match the lead. An empty `lead_ids` array selects none and writes no thesis file.

A stage with nothing left to judge is skipped. The run then finishes with zero rankings and no thesis files.

## What an agent reads

```text
data/agent_queue/<pipeline_run_id>/<stage>/<lead_id>.task.json
```

Significance has no lead row yet, so `<lead_id>` is `figure-<figure_id>`. Centurion and Praetorian stages use the sweep lead key (`1`, `2`, …). Ranking assigns the database `lead_id`. The Legate task id is the literal `legate_selection`, and the ids inside its result are those database lead ids.

The JSON object contains:

- `run_id`, `stage`, `lead_id`
- `subject` — ticker, lead type, company
- `prompt` — the full instruction, including the fields the judgement is allowed to use
- `rules` — `Centurions/rules/<type>.md` for significance and Centurion tasks (every type on a merged lead). Praetorian tasks carry `Praetorians/README.md`
- `evidence` — the figure, counterparties, and criteria. A capex chain or smart-money lead also lists `constituents`. A macro lead lists `related_findings`. A leftover merge lists its constituents on pass 2
- `macro_context` — the cached macro summary on a capex-chain Centurion, on centurion pass 2, and on a praetorian review of that thesis. It is null on significance and on a single lead. A single lead must not apply the macro cache
- `output_schema` — the object the result file must match. Extra keys are rejected

## What an agent writes

Write one JSON object, and nothing else, to the sibling file:

```text
data/agent_queue/<pipeline_run_id>/<stage>/<lead_id>.result.json
```

`ingest` rejects the whole stage if any result is missing, is not a JSON object, or breaks the schema. The error names the lead id and the field. A failed ingest does not advance, so the same command can be run again after the file is fixed.

### Combined scouts

Data-collection scouts stay separate modules. After significance, the sweep combines leads:

- **Capex chain** — capex, orders backlog, and debt raises. A spender links to a supplier when the capex names that supplier, or the backlog names that buyer, and a debt raise on either ticker joins the same chain. The result is one lead (`source_scout` `capex_chain`) whose summary names the spender, the supplier backlog, and the funding raise, with each source URL. An unlinked figure stays a single lead. These types are not merged again after the thesis.
- **Smart money** — insider buying and accumulation on the same ticker whose event dates fall inside the 90-day window. One lead (`source_scout` `smart_money`) names Form 4, 13D/13G, or ASX 604. A single signal, or two signals further apart than the window, stays separate.
- **Macro** — one lead, unchanged. Keywords in `Centurions/rules/macro_relevance.md` choose which chain and smart-money findings are copied into that Centurion task. Brent, WTI, and the Brent-WTI spread use refiner, tanker, oil, petroleum, and crude. A series with no section matches only when the finding text contains the series id.

The Centurion still writes one result file. There is no shared evidence file and no team dossier.

### Significance

`magnitude` is `large`, `moderate`, or `small`. `novelty` is `first` or `routine`. `attention` is `unnoticed` or `noticed`. `timing` is `early`, `on_time`, or `late`. Each `*_rationale` is a non-empty string.

### Centurion (pass 1 and pass 2)

`thesis`, `roi`, and `recommendation` are strings. `roi_pct` is a number. `timeframe_days` is an integer. `risks` and `assumptions` are arrays of strings. `suppliers`, `customers`, and `buyers` are arrays of `{name, ticker}`. `price_run` is null on a single lead. On a capex chain, a smart-money lead, and pass 2 it is `new`, `acceleration`, `priced-in`, or null. `conviction` is optional, one short phrase. The prompt includes GATE_RESULTS from the scout gate. The result does not grade the rubric.

### Praetorian

`flaws` is an array of `{flaw, severity, evidence}`. `severity` is `low`, `moderate`, or `high`. `flaw` and `evidence` are non-empty strings. The array may be empty. Extra keys are rejected. The list is stored and shown. It does not reject the lead.

### Legate selection

`lead_ids` is an array of at most 5 integers. `rationale` is an object with one non-empty string for each chosen id. The key is the lead id as text. Extra keys are rejected. An empty array selects none.

```text
data/agent_queue/<pipeline_run_id>/legate_selection/legate_selection.result.json
```

## After the last stage

Ranking reads the ingested expected return, timeframe, and the deterministic surviving issues. It writes `rankings` for every lead that still has a thesis. A Praetorian flaw does not remove a row from `rankings`. The same list is written to `data/reports/<run_id>-candidates.md` when the queue is `data/agent_queue` (the report directory is the queue directory's sibling `reports/`). Each section starts with a price table, then a bottom line, the cause to effect bullets, the thesis, the evidence links, the risks, the assumptions, the Praetorian table (severity, flaw, and evidence, high first), the Legate rationale, and the matching macro series. The Legate selection then writes one file per chosen lead at `data/reports/<run_id>/<rank>-<ticker>.md`, with the rationale for each pick. The sweep does not write a decision row. `pipeline_runs.status` becomes `complete` and `stage` becomes `legate_selection`. `provider` is `agent`. Token columns stay at 0.

A judgement reject is stored with `rejection_stage` `agent`. A threshold or dedupe reject stays `deterministic` and is written by `run`, before any agent task.
