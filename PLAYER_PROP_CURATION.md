# Player Prop Curation

The current player-prop models are not limited only by modeling choices. They are also limited by how training data is assembled.

Right now, most player-prop training samples are still generated inline inside `backend/app/player_prop_model.py`. That makes iteration slower because:

- cleanup rules are harder to audit
- sample counts are harder to inspect by market
- residual/final evaluation inputs are not materialized in one place
- future feature changes are harder to validate against a stable training snapshot

## Direction

Player props should follow the same persistence pattern as minutes and game training:

- live raw data in `/data/wnba.sqlite`
- curated examples in `/data/wnba-training.sqlite`
- model-specific tables inside the shared training DB

## Curated Tables

Initial curated player-prop tables:

- `player_prop_training_metadata`
- `player_prop_training_examples`

The first scaffold stores three sample kinds:

- `raw`: direct historical target rows used for base market models
- `residual`: line-relative settled rows used for residual training
- `final`: settled evaluation rows used for final projection validation

It now also stores explicit market-curation exclusions in:

- `player_prop_training_exclusions`

## Why This Matters

This does not automatically make the model elite. It does make the next rounds of improvement real instead of opaque.

With curated player-prop training data we can:

- inspect candidate vs included vs excluded rows by market
- identify weak markets with low-quality history windows
- add market-specific exclusion rules without burying them in training code
- compare new feature sets against the same saved training population
- version dataset changes explicitly
- audit market-specific exclusions by reason

## Immediate Next Steps

1. Materialize the current inline sample sets into the shared training DB.
2. Inspect row counts by market and sample kind.
3. Add explicit exclusion-reason rows, not just aggregate diagnostics.
4. Move model training loaders from inline sample generation to curated DB reads.
5. Add market-specific data cleanup:
   - unreliable low-minute rows
   - line anomalies
   - stale/misaligned odds
   - duplicate or conflicting settled records
6. Add probability-calibration datasets per market after raw/regression datasets are stable.

## Current Status

Implemented:

- scaffolded `backend/app/player_prop_training_db.py`
- scaffolded:
  - `scripts/build_player_prop_training_db.py`
  - `scripts/inspect_player_prop_training_db.py`
- extended sample objects so curated rows can carry:
  - `source_player_id`
  - `source_game_id`
- added explicit exclusion persistence for market-curation rules
- added first market-specific curation pass for:
  - `points`
  - `points_rebounds`
  using recent-transfer and sample-quality thresholds on `raw` and `residual` training rows

Still not finished:

- sample-level exclusion persistence only covers post-build market curation, not every inline skip reason
- line-quality and stale-odds cleanup still need dedicated rules
- recent-transfer handling still needs a live inference fallback policy, not just cleaner training rows

## Standard Command Pattern

Build on the live mounted volume:

```bash
python scripts/live_backend.py exec -- /bin/sh -c 'PYTHONPATH=/app python /app/scripts/build_player_prop_training_db.py --force'
```

Inspect on the live mounted volume:

```bash
python scripts/live_backend.py exec -- /bin/sh -c 'PYTHONPATH=/app python /app/scripts/inspect_player_prop_training_db.py'
```
