# WNBA Model Audit - 2026-08-21

## Scope

This audit focuses on:

- game predictions
- player props
- projection logic versus actual settled outcomes
- whether the current models need improvement
- whether ESPN and Rotowire team/context data are being used appropriately

## Executive Summary

The project does not primarily have a "missing model" problem. It has a calibration, freshness, and market-governance problem.

The strongest conclusions are:

- ESPN team stats are being persisted and used appropriately as the team-stat backbone.
- Rotowire is not being used as a team-stat source; it is being used as an injury / lineup-availability source, which is appropriate.
- The game model feature set is directionally strong, but injury context was not consistently wired through every game-related path.
- The player prop model has reasonable structure, but confidence labels and specialty-market handling need stronger guardrails.
- Operational freshness matters as much as model design. Stale or partially refreshed training artifacts will undermine otherwise sound feature engineering.

## Live Outcome Snapshot

From the live database review on 2026-08-21:

- `adaptive-context-v1`: MAE `4.065`, RMSE `5.756`, bias `-0.057`, directional accuracy `52.2%`, edge>=5% directional accuracy `53.8%` across `1609` evaluated settled rows
- `component`: MAE `4.041`, RMSE `5.708`, directional accuracy `50.8%`
- `component-pregame-v2`: MAE `4.176`, RMSE `5.861`, directional accuracy `49.9%`

This is not evidence of a broken system, but it is also not evidence of a model that is clearly pulling away from a conservative baseline. The learned prop model is only modestly better in places, and some confidence assignments are overstating certainty.

Confidence calibration was a bigger concern than raw mean error:

- most evaluated rows were tagged `low`
- the small `high` bucket had strong directional hit rate, but large negative bias and poor absolute error, which suggests the gate was not cleanly identifying "high quality" projections

## Data Source Assessment

### ESPN team stats

ESPN is the actual team-stat source in this project.

Evidence:

- final game-level team rows are written into `team_game_results` in [backend/app/espn_history.py](/root/WNBAStats/backend/app/espn_history.py:145)
- richer boxscore team rows are written into `team_game_boxscores` in [backend/app/espn_history.py](/root/WNBAStats/backend/app/espn_history.py:691)
- schema support for both tables exists in [backend/app/db.py](/root/WNBAStats/backend/app/db.py:1226) and [backend/app/db.py](/root/WNBAStats/backend/app/db.py:1243)

What is being persisted:

- points and opponent points
- possessions and possession source
- rebounds, offensive rebounds, defensive rebounds
- assists, steals, blocks
- turnovers, team turnovers, total turnovers
- field-goal and free-throw attempt/make counts

This is the correct approach. Team stats change after every completed game, so they should be persisted after each daily update and then used as features from the stored tables instead of fetched ad hoc at inference time.

### Rotowire

Rotowire is not the team-stat source here and should not be treated as one.

Evidence:

- Rotowire ingestion is lineup / injury parsing in [backend/app/rotowire_import.py](/root/WNBAStats/backend/app/rotowire_import.py:1)
- it writes availability records into `injuries`, not team-performance tables, in [backend/app/rotowire_import.py](/root/WNBAStats/backend/app/rotowire_import.py:80)

This is appropriate usage. Rotowire is being used for:

- unavailable player statuses
- roster / lineup change detection
- injury freshness and fallback cache behavior

That is where Rotowire adds value. It should remain context, not core stats.

## How Team Stats Feed Features

### Game model

The game model uses team-level and matchup-level pregame features via [backend/app/game_pregame_features.py](/root/WNBAStats/backend/app/game_pregame_features.py:243).

Those features include:

- projected team minutes, points, rebounds, assists
- rotation concentration and creator counts
- team form deltas
- pace, FGA, turnover, and volatility deltas
- hot / slump regime flags

The team form regime is built from stored `team_game_results` and `team_game_boxscores` in [backend/app/team_form_regime.py](/root/WNBAStats/backend/app/team_form_regime.py:12). That is exactly the right pattern: derive features from persisted historical team tables.

### Player prop model

The player prop model feature set in [backend/app/player_prop_model.py](/root/WNBAStats/backend/app/player_prop_model.py:21) already incorporates a substantial amount of contextual information:

- pace and opponent factors
- position allowance context
- transfer / new-team context
- teammate availability redistribution
- team and opponent form deltas
- volatility and role signals

Conceptually, this is a good feature surface. The current issues are mostly around calibration, support thresholds, and operational freshness rather than an obviously missing feature family.

## Key Improvement Areas

### 1. Confidence calibration needs work

This is the clearest modeling issue.

The project already evaluates settled outcomes through `settled_props` and `settled_game_predictions` in [backend/app/main.py](/root/WNBAStats/backend/app/main.py:3304) and [backend/app/main.py](/root/WNBAStats/backend/app/main.py:3377). That means the system has the right feedback loop, but the confidence policy was too permissive.

Why this matters:

- users act on confidence buckets, not only on raw projections
- a model with average MAE but good calibration is more useful than a model with similar MAE and noisy confidence labels

Recommendation:

- keep confidence gates strict
- validate confidence by market, not only overall
- track calibration separately for `low`, `medium`, and `high`

### 2. Specialty prop markets need conservative handling

Markets such as `steals`, `blocks`, and `blocks_steals` are structurally low-volume and higher-variance.

Evidence:

- training markets include these specialty categories in [backend/app/player_prop_model.py](/root/WNBAStats/backend/app/player_prop_model.py:21)
- specialty-market overlay logic is controlled in [backend/app/player_prop_model.py](/root/WNBAStats/backend/app/player_prop_model.py:664)

These markets should not receive the same learned-overlay trust as `points`, `rebounds`, or `assists` unless there is clear residual and final-support coverage.

Recommendation:

- keep specialty markets on conservative / component logic unless they pass explicit support thresholds
- evaluate them as a separate model class, not as a normal extension of the core prop model

### 3. Injury context should be consistent across game products

Game training and direct game prediction now rely on matchup features that can use injury context, and that is correct.

Evidence:

- game training DB build uses `use_injury_context=True` in [backend/app/game_training_db.py](/root/WNBAStats/backend/app/game_training_db.py:294)
- direct game predictions use `use_injury_context=True` in [backend/app/game_predictions.py](/root/WNBAStats/backend/app/game_predictions.py:737)

But not every adjacent path is aligned:

- segment prediction features still use `use_injury_context=False` in [backend/app/segment_predictions.py](/root/WNBAStats/backend/app/segment_predictions.py:455)

Recommendation:

- standardize injury-context usage across full-game and segment models
- avoid having the same matchup described differently across related products

### 4. Training and artifact freshness need to be treated as first-class model quality issues

The model can only be as current as:

- the persisted ESPN team results / boxscores
- settled outcomes
- refreshed curated training tables
- the published model artifacts actually used at inference time

This is not "just operations." It directly affects model quality because stale training inputs and stale artifacts create false confidence about feature improvements that are not present in live predictions.

Recommendation:

- continue loading ESPN game/team stats daily
- refresh derived training tables from persisted data on a predictable schedule
- separate "data refresh" from "full retrain" so the pipeline stays debuggable

### 5. The learned model is not yet clearly dominant over the component baseline

The live evaluation snapshot showed only modest separation between the learned prop model and the conservative component baselines.

That means:

- the learned model may still be helping, but
- the current implementation does not justify aggressive trust in every market

Recommendation:

- promote learned overlays market by market
- gate promotion on settled-outcome evidence, not only on walk-forward training metrics

## Do The Models Need Improvement?

Yes, but not in the simplistic sense of "replace the model with a bigger one."

The models need improvement in this order:

1. calibration and recommendation policy
2. market-specific governance
3. consistent injury-context usage
4. training / artifact freshness
5. only then additional feature or architecture experimentation

The current feature sets are already reasonably rich. The next gains are more likely to come from:

- better market segmentation
- better confidence calibration
- better support thresholds
- cleaner live-data freshness

than from a wholesale rewrite of the regressors.

## Are ESPN And Rotowire Being Used Appropriately?

### ESPN

Yes. ESPN team data is being used in the right role:

- persisted to historical team tables
- converted into stable features
- reused across game and player-context modeling

The correct operational policy is to update those tables daily after each slate because team stats change after every completed game.

### Rotowire

Yes, mostly. Rotowire is being used for:

- injury / lineup availability
- cache-backed roster context

That is the right role. It should not be elevated into a core team-stat source.

## Ranked Next Steps

### High impact

1. Keep ESPN team stats as daily persisted inputs and explicitly monitor update freshness.
2. Treat confidence calibration as a core model output and review it by market.
3. Keep specialty markets on conservative logic until their settled support is strong enough.
4. Align injury-context usage across full-game and segment prediction paths.

### Medium impact

1. Split model evaluation more clearly by market family:
   - core counting stats
   - combo stats
   - specialty stocks markets
2. Add promotion criteria that compare learned models against the component baseline on settled results, not only backtests.
3. Keep the data-refresh pipeline lightweight enough that it can run daily without forcing a long retrain.

### Lower impact

1. Explore additional team-feature summaries only after the calibration and governance issues are stabilized.
2. Add more formal model cards / run summaries for promoted model versions.

## Bottom Line

The project is using ESPN and Rotowire in the right conceptual roles.

The biggest improvements needed are:

- better confidence calibration
- stronger specialty-market guardrails
- consistent injury-context wiring
- disciplined daily persistence and reuse of ESPN team stats

The evidence does not say "throw away the models." It says:

- keep the current feature direction
- improve the decision policy around those features
- make freshness and market-specific controls part of the modeling standard
