# Agent protocol

The live sweep does not call a model and does not read an API key. `python -m pipeline run` does the deterministic work, writes one task file per judgement, records `awaiting_agents`, and exits 0. An external agent reads each task and writes a result file. `python -m pipeline ingest` checks those files and advances.

Tests drive the same queue with `pipeline.fixture.run_with_fixture`. That responder is not imported by the live commands.

## Commands

```bash
python3 -m pipeline run --db /tmp/cause-n-effect.sqlite
python3 -m pipeline status --db /tmp/cause-n-effect.sqlite --run <run_id>
python3 -m pipeline ingest --db /tmp/cause-n-effect.sqlite --run <run_id>
```

Repeat `status` and `ingest` after each stage. The last successful `ingest` prints `status: complete`, `stage: ranking`, and the ranking and decision counts.

`--queue-dir` overrides the default `data/agent_queue`. `ingest` must use the same directory the run stored.

`--dry-run` prints how many significance tasks would be queued. It writes no queue files, no rejects, and no `pipeline_runs` row.

When `SEC_CONTACT_EMAIL` is set, `run` caches shares outstanding and `ingest` names counterparties after significance. The address is the SEC User-Agent and is not logged. A source error drops that company's parties. With the variable unset, neither step opens a socket.

A market cap is shares outstanding times an official USD share-price figure already stored with the scout figures. Shares without that price leave the cap unverified.

## Stages

1. **significance** — one task per figure that passed thresholds and dedupe, and one task per macro signal.
2. **centurion_1** — one task per lead after the deterministic combine. A capex chain (capex, orders backlog, and debt where they link) is one lead. Smart money (insider and accumulation on the same ticker inside the window) is one lead. Macro stays one lead, and its task lists related findings from the other two. An unlinked figure stays a single lead.
3. **Legate merge** — deterministic, no task file. Chain types are not merged again. A leftover cross-group link, such as capex with insider buying, becomes one merged lead.
4. **centurion_2** — one task per leftover merged lead. A sweep with no leftover merge skips this stage.
5. **praetorian** — one task per lead that still has a thesis (the chain or smart-money lead, a single lead, or a leftover merge). The result is a list of flaws. Each flaw has `flaw`, `severity` (`low`, `moderate`, or `high`), and `evidence`. An empty list is valid. The critique does not reject, drop, or filter a lead.
6. **ranking** — deterministic. Score is expected return, plus growth and magnitude, minus 8 points per deterministic surviving issue (a significance soft issue, an unverified market cap, or an open backlog growth question). Praetorian flaws are not part of that count and do not remove a lead. The top of the list, at most `--max-theses` (default 5), gets a pending decision and a section in `data/reports/<run_id>.md`. Each section includes that thesis's full flaw list. No task file.

A stage with nothing left to judge is skipped. The run then finishes with zero rankings and zero decisions.

## What an agent reads

```text
data/agent_queue/<pipeline_run_id>/<stage>/<lead_id>.task.json
```

Significance has no lead row yet, so `<lead_id>` is `figure-<figure_id>`. Centurion and Praetorian stages use the sweep lead key (`1`, `2`, …). The database `lead_id` is assigned when ranking finishes.

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

`thesis`, `roi`, and `recommendation` are strings. `roi_pct` is a number. `timeframe_days` is an integer. `risks` and `assumptions` are arrays of strings. `grades` is an array of `{name, passed, evidence}` with `passed` true or false. `suppliers`, `customers`, and `buyers` are arrays of `{name, ticker}`. `price_run` is null on a single lead. On a capex chain, a smart-money lead, and pass 2 it is `new`, `acceleration`, `priced-in`, or null.

### Praetorian

`flaws` is an array of `{flaw, severity, evidence}`. `severity` is `low`, `moderate`, or `high`. `flaw` and `evidence` are non-empty strings. The array may be empty. Extra keys are rejected. The list is stored and shown. It does not reject the lead.

## After the last stage

Ranking reads the ingested expected return, timeframe, and the deterministic surviving issues. It writes `rankings` for every lead that still has a thesis and a `pending` row in `decisions` for each presented lead. A Praetorian flaw does not remove a row from `rankings`. `--max-theses` on `run` caps that presented list (default 5). The same list is written to `data/reports/<run_id>.md` when the queue is `data/agent_queue` (the report directory is the queue directory's sibling `reports/`). Each section has the thesis, the cause to effect chain, the tickers, the evidence links, the full Praetorian flaw list (severity, flaw, and evidence), and the macro context. `pipeline_runs.status` becomes `complete` and `stage` becomes `ranking`. `provider` is `agent`. Token columns stay at 0.

A judgement reject is stored with `rejection_stage` `agent`. A threshold or dedupe reject stays `deterministic` and is written by `run`, before any agent task.
