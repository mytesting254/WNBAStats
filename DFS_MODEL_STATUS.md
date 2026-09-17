# DFS First-Half Model Status

Updated: 2026-09-17

## Current model

The DFS subsystem predicts first-half player prop outcomes from full-game prop
context, player projections, first-half history, minutes, pace, and role data.
It stores pregame estimates in `dfs_first_half_projection_snapshots` and grades
completed games in `dfs_first_half_projection_settlements`.

The current shipped model version is `dfs-first-half-ridge-v5`. Its training
database version is `v4`.

## Validated improvement

The current model adds two historical opportunity proxies:

- first-half threes per field-goal attempt;
- first-half assists per field-goal attempt.

These are derived only from games before the prediction date and are added to
both the training and live-serving feature paths. They are intended to capture
opportunity and role more directly than raw recent makes or assists.

The training database rebuilds automatically when its version or source
signature changes. Model and evaluation cache versions were also bumped so old
feature-width artifacts are not reused.

## Walk-forward result

On the rebuilt evaluation copy containing 15,415 chronological samples:

| Metric | Previous baseline | Current model |
| --- | ---: | ---: |
| Directional accuracy | 71.1% | 72.9% |
| MAE | 1.983 | 1.961 |

Selected current market results:

| Market | Samples | Directional accuracy | MAE |
| --- | ---: | ---: | ---: |
| Points | 3,541 | 63.3% | 2.894 |
| Rebounds | 2,720 | 80.1% | 0.942 |
| Assists | 1,070 | 55.7% | 1.213 |
| Points + rebounds | 2,396 | 77.4% | 2.558 |
| Points + assists | 1,656 | 75.4% | 2.710 |
| Rebounds + assists | 1,198 | 75.2% | 1.361 |
| Points + rebounds + assists | 1,104 | 81.9% | 2.530 |
| Threes | 1,730 | 76.2% | 0.626 |

Turnovers remain unshipped because the observed first-half sample is too small.

These are model walk-forward metrics against the synthetic halfway line derived
from the full-game line. They are not sportsbook ROI or proof of profitability.

## Experiments rejected

Recent-vs-line and recent-vs-component interaction features improved assists
slightly but reduced overall accuracy, so they were reverted.

Explicit teammate-redistribution interactions were neutral in the available
sample and were also reverted. The underlying historical data contains too
little usable lineup-redistribution variation to justify shipping those extra
features yet.

## Next data improvement

True first-half three-point attempts are the highest-value next feature. The
current `threes / FGA` proxy mixes shot volume and shooting accuracy. A safe
implementation requires coordinated changes to:

1. ESPN play-by-play parsing;
2. `player_first_half_stats` schema and migration;
3. the player-half training database;
4. historical feature construction;
5. live feature construction;
6. chronological re-evaluation before promotion.

Potential assists and touches would be useful for assists, but the current ESPN
feed does not expose them reliably enough to add without a separate validation
and fallback design.

## Validation commands

Focused DFS tests:

```bash
.venv/bin/pytest -q backend/tests/test_projection.py -k 'dfs or player_half'
```

Walk-forward evaluation:

```bash
python scripts/evaluate_dfs_first_half_models.py
```
