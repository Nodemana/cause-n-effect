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

## Stages

1. **significance** — one task per figure that passed thresholds and dedupe.
2. **centurion_1** — one task per surviving lead.
3. **Legate merge** — deterministic, no task file. Linked theses become one merged lead.
4. **centurion_2** — one task per merged lead. A sweep with no merge skips this stage.
5. **praetorian** — one task per lead that still has a thesis after the merge (the merged lead, or the unmerged lead).
6. **ranking** — deterministic. Score is expected return, plus growth and magnitude, minus 8 points per surviving issue. The top of the list gets a pending decision. No task file.

A stage with nothing left to judge is skipped. The run then finishes with zero rankings and zero decisions.

## What an agent reads

```text
data/agent_queue/<pipeline_run_id>/<stage>/<lead_id>.task.json
```

Significance has no lead row yet, so `<lead_id>` is `figure-<figure_id>`. Later stages use the sweep lead key (`1`, `2`, …). The database `lead_id` is assigned when ranking finishes.

The JSON object contains:

- `run_id`, `stage`, `lead_id`
- `subject` — ticker, lead type, company
- `prompt` — the full instruction, including the fields the judgement is allowed to use
- `rules` — `Centurions/rules/<type>.md` for significance and Centurion tasks (every type on a merged lead). Praetorian tasks carry `Praetorians/README.md`
- `evidence` — the figure, counterparties, criteria, and on a second pass the merged constituents
- `macro_context` — the cached macro summary on centurion pass 2 and on a praetorian review of that pass. It is null on significance and on centurion pass 1. Those stages must not apply macro
- `output_schema` — the object the result file must match. Extra keys are rejected

## What an agent writes

Write one JSON object, and nothing else, to the sibling file:

```text
data/agent_queue/<pipeline_run_id>/<stage>/<lead_id>.result.json
```

`ingest` rejects the whole stage if any result is missing, is not a JSON object, or breaks the schema. The error names the lead id and the field. A failed ingest does not advance, so the same command can be run again after the file is fixed.

### Significance

`magnitude` is `large`, `moderate`, or `small`. `novelty` is `first` or `routine`. `attention` is `unnoticed` or `noticed`. `timing` is `early`, `on_time`, or `late`. Each `*_rationale` is a non-empty string.

### Centurion (pass 1 and pass 2)

`thesis`, `roi`, and `recommendation` are strings. `roi_pct` is a number. `timeframe_days` is an integer. `risks` and `assumptions` are arrays of strings. `grades` is an array of `{name, passed, evidence}` with `passed` true or false. `suppliers`, `customers`, and `buyers` are arrays of `{name, ticker}`. `price_run` is null on pass 1. On pass 2 it is `new`, `acceleration`, `priced-in`, or null.

### Praetorian

`assumptions_that_fail`, `bad_sources`, `contradicting_research`, `other_side`, and `price_run_counterevidence` are strings and may be empty. `surviving_issues` is an integer, zero or greater. `price_run_stance` is `agree`, `disagree`, or empty.

## After the last stage

Ranking reads the ingested expected return, timeframe, and surviving issues. It writes `rankings` and a `pending` row in `decisions` for each presented lead. `pipeline_runs.status` becomes `complete` and `stage` becomes `ranking`. `provider` is `agent`. Token columns stay at 0.

A judgement reject is stored with `rejection_stage` `agent`. A threshold or dedupe reject stays `deterministic` and is written by `run`, before any agent task.
