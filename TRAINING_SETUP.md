# Training Setup Pattern

This document defines the standard setup for any new trainable model in this repo.

The goal is simple:

- raw app/runtime data stays in the live app DB
- cleaned training examples live in the shared training DB
- both files live on the mounted volume
- training code reads curated examples instead of rebuilding ad hoc logic every run

## Storage Rules

Use the mounted runtime volume for all persistent training assets.

Runtime defaults:

- app DB: `/data/wnba.sqlite`
- shared training DB: `/data/wnba-training.sqlite`
- cache/artifacts: `/data/cache`
- snapshots: `/data/snapshots`

Host path for the live mounted volume in the current deployment:

- `/var/lib/docker/volumes/pqsez6mkr14y0bnlhmcdmikg_wnba-data/_data`

Do not make repo-local `data/*.sqlite` the default for new training flows.
Repo-local SQLite files are only for temporary local validation.

## Required Pattern For New Models

Every new trainable model should follow this structure:

1. Raw source data remains in `wnba.sqlite`.
2. Add a curated builder module in `backend/app/`.
3. Write cleaned examples into `wnba-training.sqlite`.
4. Keep model-specific tables separate inside the shared training DB.
5. Expose a build script in `scripts/`.
6. Expose an inspect script in `scripts/`.
7. Add a metadata/signature function so retraining invalidates cleanly when source data changes.
8. Train the live model from curated rows, not from inline history reconstruction.

## Table Naming

Use one metadata table and one examples table per model family.

Examples:

- minutes:
  - `minutes_training_metadata`
  - `minutes_training_examples`
- game:
  - `game_training_metadata`
  - `game_training_examples`

If a future model needs multiple example tables, keep the metadata table singular and namespace the example tables clearly.

## Builder Module Contract

Each curated training builder should provide:

- `ensure_<model>_training_db(conn, force=False)`
- `load_<model>_training_rows(...)` or `load_<model>_training_examples(...)`
- `<model>_training_db_signature(conn)`

The builder should:

- create its tables if missing
- compute a source signature from the live app DB
- skip rebuild when signature matches and rows already exist
- rebuild cleanly when forced or when the source signature changes
- record candidate, included, and excluded row counts
- store exclusion counts and build timestamp in metadata

## Example Row Requirements

Each curated example table should store:

- source entity ids
- event date / season
- feature payload used for model training
- target value(s)
- quality flags
- clean/excluded status
- exclusion reason
- created timestamp

For market-based models, also store any market context needed to reproduce training targets later, such as:

- spread
- total
- moneyline
- implied probabilities

## Cleaning Rules

Training rows must be pregame-safe.

That means:

- features may only use information available before the target event
- no future results leakage
- no postgame corrections baked into features

Rows can be excluded when they fail minimum quality standards, for example:

- not enough history
- missing team resolution
- missing market line
- invalid target
- pre-training-start date

Excluded rows should remain visible in the curated DB with `is_clean = 0` and an `exclusion_reason`.

## Script Pattern

Every new model family should have:

- `scripts/build_<model>_training_db.py`
- `scripts/inspect_<model>_training_db.py`

Build script responsibilities:

- use active `WNBA_DB_PATH` by default
- use mounted-volume `WNBA_TRAINING_DB_PATH` by default
- support `--force`
- print path and row-count summary

Inspect script responsibilities:

- print metadata
- print rows by season / subgroup when relevant
- print exclusion counts
- print a small sample of excluded rows

## Runtime Path Rules

New training scripts must default to the active runtime pathing.

Do this:

- respect `WNBA_DB_PATH`
- respect `WNBA_TRAINING_DB_PATH`
- default training DB beside `WNBA_DB_PATH`

Do not do this:

- hardcode repo-local `data/wnba.sqlite`
- hardcode repo-local `data/wnba-training.sqlite`

## Training Integration

Once the curated DB exists, the model code should train from it directly.

Preferred pattern:

- builder populates curated tables
- model loader reads curated rows
- model signature includes curated training DB signature

This is better than rebuilding historical rows inside the prediction module every run because:

- cleanup logic is centralized
- row counts are inspectable
- exclusions are explicit
- rebuilds are deterministic
- future migrations are easier

## Operational Commands

Load live runtime paths on the host:

```bash
source scripts/live_env.sh
```

Build minutes training DB on the live mounted volume:

```bash
python scripts/live_backend.py exec -- /bin/sh -c 'PYTHONPATH=/app python /app/scripts/build_minutes_training_db.py --force'
```

Build game training DB on the live mounted volume:

```bash
python scripts/live_backend.py exec -- /bin/sh -c 'PYTHONPATH=/app python /app/scripts/build_game_training_db.py --force'
```

Inspect the shared training DB from inside the live container:

```bash
python scripts/live_backend.py exec -- python -c "import sqlite3; conn=sqlite3.connect('/data/wnba-training.sqlite'); print(conn.execute(\"SELECT name FROM sqlite_master WHERE type='table' ORDER BY name\").fetchall())"
```

Verify game-training metadata on the live mounted volume:

```bash
python scripts/live_backend.py exec -- python -c "import sqlite3, json; conn=sqlite3.connect('/data/wnba-training.sqlite'); conn.row_factory=sqlite3.Row; print(json.dumps([dict(r) for r in conn.execute(\"SELECT key, value FROM game_training_metadata\").fetchall()], indent=2))"
```

Verify game-training row counts on the live mounted volume:

```bash
python scripts/live_backend.py exec -- python -c "import sqlite3, json; conn=sqlite3.connect('/data/wnba-training.sqlite'); print(json.dumps({'game_training_examples': conn.execute(\"SELECT COUNT(*) FROM game_training_examples\").fetchone()[0], 'game_training_clean': conn.execute(\"SELECT COUNT(*) FROM game_training_examples WHERE is_clean = 1\").fetchone()[0], 'game_training_excluded': conn.execute(\"SELECT COUNT(*) FROM game_training_examples WHERE is_clean = 0\").fetchone()[0]}, indent=2))"
```

Live verification baseline from July 14, 2026:

- `game_training_examples`: `790`
- `game_training_clean`: `503`
- `game_training_excluded`: `287`
- `exclusion_counts`: `before_training_start=273`, `missing_history_window=14`

## Future Model Checklist

Before calling a new training flow complete, verify all of the following:

1. Raw source data already exists in `wnba.sqlite`.
2. Curated builder module exists in `backend/app/`.
3. Shared training DB tables are created in `/data/wnba-training.sqlite`.
4. Rebuild script exists.
5. Inspect script exists.
6. Metadata includes build time, counts, exclusions, and source signature.
7. Training code reads curated rows instead of inline reconstruction.
8. Model-run signatures include the curated DB signature.
9. Output persists on the mounted volume.
10. Live verification confirms tables, metadata, and row counts on the mounted volume.
11. The setup is documented here or linked from here.

## Current Implementations

Current curated training families:

- minutes
- game

Minutes is the reference pattern.
Game should follow the same persistence, metadata, and inspection pattern.
