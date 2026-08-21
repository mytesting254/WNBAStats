# Deploy vs Retrain - 2026-08-21

## Current Recommendation

Do not start with a full retrain.

Deploy the current runtime projection changes first, then re-evaluate fresh settled results before deciding whether a full retrain is still necessary.

## Why

The largest recent improvements came from runtime projection-path changes, not from refitting model weights:

- projection safety floors
- role/minutes fallback
- stronger combo stabilization for `points_rebounds`, `points_assists`, and `points_rebounds_assists`
- star-usage ceiling preservation
- stricter confidence and value-board policy for weak scoring markets
- explicit minutes-cohort and scoring-market diagnostics

These changes alter live projection behavior immediately once deployed.

## What Requires Deployment Only

- `backend/app/player_prop_model.py`
- `backend/app/projections.py`
- `backend/app/main.py`
- related game-model context wiring and reporting updates

These are runtime logic changes and do not need a retrain to take effect.

## What Could Justify Retraining Later

Retrain only after the deployed code has produced fresh settled rows and the normal-minute scoring diagnostics still show material weakness.

Likely retrain triggers:

- `points` normal-minute projection error still flat after deployment
- `points_rebounds` / `points_assists` catastrophic tail still too large
- learned overlays still underperform the conservative component path after the runtime fixes

## Review Workflow

After deployment, check:

1. `/api/model-performance`
2. `/api/projection-accuracy-review?model_version=adaptive-context-v1&min_minutes=11&markets=points,points_rebounds,points_assists,points_rebounds_assists`
3. `/api/projection-accuracy-review?model_version=component-pregame-v2&min_minutes=11&markets=points,points_rebounds,points_assists,points_rebounds_assists`

Focus on:

- normal-minute scoring-market MAE
- normal-minute scoring-market bias
- `miss8_rate`
- `miss12_rate`
- worst-miss tail by market

## Bottom Line

Deploy first.

Use fresh settled normal-minute scoring diagnostics to decide whether retraining is still justified.
