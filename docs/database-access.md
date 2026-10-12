# Database access

The live history database is a single SQLite file kept on the orchestrator's machine. After each scout run, a read-only snapshot is copied to the owner's workstation at `~/Projects/cause-n-effect/cause-n-effect-scouts.db`. Later snapshots go to that same path.

The snapshot is a copy. Edits there do not flow back to the live file.

## Datasette

Datasette is installed in its own virtual environment at `~/Projects/cause-n-effect/.venv`. From `~/Projects/cause-n-effect`:

```bash
python3 -m venv .venv && .venv/bin/pip install datasette
```

Open the snapshot with `~/Projects/cause-n-effect/view-db.sh`:

```sh
#!/bin/sh
D="$HOME/Projects/cause-n-effect"
exec "$D/.venv/bin/datasette" "$D/cause-n-effect-scouts.db" -h 127.0.0.1 -p 8001 --open
```

The script binds to localhost only. The browser opens `http://127.0.0.1:8001`, where every table can be browsed, filtered, sorted, and queried with SQL.

## DB Browser for SQLite

DB Browser for SQLite can open `~/Projects/cause-n-effect/cause-n-effect-scouts.db` directly. That is the same copy Datasette serves, and changes in either tool stay on the workstation.

## Tables

- `runs`
- `figures`
- `leads`
- `scout_tests`
- `debt_scout_runs`
- `debt_scout_filing_scores`
- `schema_migrations`
- `macro_series`
- `macro_observations`
- `changes` (view)
