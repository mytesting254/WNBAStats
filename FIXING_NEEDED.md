# Model Fixing Needed

This is the consolidated model-work index for the repo.

Use this document to separate:

- active model fixes
- training/data-pipeline work
- prop ingestion refactor
  see `PROP_INGESTION_REFACTOR.md` for the shared provider-ingestion plus downstream prop-sync pipeline split
- evaluation/reporting work
- operational follow-up

Do not use this file as a changelog. Completed work should stay in the source docs or commit history. This file is for pending and current work only.

## Source Docs And Ownership

Use each document for one purpose:

- [README.md](/root/WNBAStats/README.md)
  - product behavior
  - current live model behavior
  - operator workflow
  - high-level training/evaluation notes
- [TRAINING_SETUP.md](/root/WNBAStats/TRAINING_SETUP.md)
  - required pattern for any new trainable model
  - mounted-volume persistence rules
  - curated DB/signature/integration requirements
- [PLAYER_PROP_CURATION.md](/root/WNBAStats/PLAYER_PROP_CURATION.md)
  - player-prop curated dataset design
  - player-prop curation status
  - player-prop-specific remaining cleanup
- [STOCKS_PLAN.md](/root/WNBAStats/STOCKS_PLAN.md)
  - Specials/stocks-specific prep, calibration, and performance work
- [TROUBLESHOOTING.md](/root/WNBAStats/TROUBLESHOOTING.md)
  - runtime failures
  - live deployment/operator issues

## Work Separation

## Active Now

This section is the current working set. If a task is actively blocking model quality or promotion, list it here first.

### Player Props

This includes:

- regular player markets
- learned overlays
- residual models
- curated player-prop training DB

Pending work:

- keep the component baseline live until a learned overlay wins out-of-sample by market, season, and recent window
- finish the minutes-model work before adding more player-prop complexity
- improve `points` and combo-market learned performance, especially `points_rebounds`
- add full sample-level exclusion persistence for all curated player-prop skip reasons, not only post-build market curation
- add dedicated curation rules for:
  - stale/misaligned odds
  - line anomalies
  - duplicate/conflicting settled rows
  - low-quality recent-transfer situations
- add a live inference fallback policy for recent-transfer players instead of only cleaning them in training experiments
- decide whether line-relative training should remain a secondary residual layer or become a primary path for specific markets
- measure residual side changes and blend impact by market before promoting broader residual usage
- keep reviewing market-specific promotion after the current weak-market fallback pass (`points_rebounds`, `points_assists`, `rebounds_assists`, `threes` held at `component_only`)
- keep reviewing thin-margin under behavior after the current combo/scoring under-gating pass
- add stronger promotion gates:
  - minimum support
  - baseline wins
  - bias sanity
  - recent-window stability
  - calibration sanity
- save richer diagnostic slices per market:
  - month
  - role
  - line bucket
  - minutes bucket
  - transfer slice
  - injury/opportunity slice

Current source docs:

- [README.md](/root/WNBAStats/README.md)
- [PLAYER_PROP_CURATION.md](/root/WNBAStats/PLAYER_PROP_CURATION.md)
- [TRAINING_SETUP.md](/root/WNBAStats/TRAINING_SETUP.md)

### Minutes Model

This includes:

- projected minutes
- role buckets
- opportunity/competition context
- lineup/returner pressure context

Pending work:

- turn the current bucket-level regressions into wins before calling minutes finished
- improve the weak buckets specifically:
  - `rotation`
  - `starter_volatile`
  - `2026` slice
- keep slicing diagnostics by:
  - `trend_bucket`
  - `volatility_bucket`
  - `context_bucket`
  - top-loss rows for `rotation` and `starter_volatile`
- keep targeted late-stage controls narrow:
  - true vacancy should still pass through
  - unsupported `starter_volatile` rise cases can be capped in `soft_vacancy`
- keep cleanup/calibration focused on hard buckets rather than broad feature expansion
- confirm whether current row-quality cleanup is enough or whether more role-specific pruning is needed
- document final promotion criteria for minutes so it can gate downstream player-prop work cleanly

Latest status:

- diagnostics CLI added: `scripts/inspect_minutes_diagnostics.py`
- latest checked window: `2026-07-01` on `data/wnba-live-source.sqlite` (last checked `2026-07-16`)
- current production metrics:
  - overall minutes MAE: `4.330`
  - `recent_blend` MAE: `4.305`
  - `core_starter` MAE: `3.141` vs `recent_blend` `3.063`
  - `starter_volatile` MAE: `4.622` vs `recent_blend` `4.640`
  - `rotation` MAE: `4.965` vs `recent_blend` `4.945`
  - low-volatility slice: `4.201` vs `recent_blend` `4.058`
  - stable-trend slice: `4.242` vs `recent_blend` `4.134`
  - `soft_vacancy` MAE: `4.371` vs `recent_blend` `4.278`
- latest implemented refinements:
  - quiet stable-context floors for `rotation`, `starter_volatile`, and `core_starter`
  - tighter rise caps for stable and vacancy-driven `rotation` / `starter_volatile` upside
  - more recent-weighted drop baselines for `bench` / `fringe` rows
  - softer `hard rule blowout star cap` in soft-vacancy-like starter/star cases
  - measured effect across the refinement pass: overall improved from `4.397` to `4.330`
  - measured effect across the refinement pass: `starter_volatile` improved from `4.787` to `4.622`
  - measured effect across the refinement pass: `rotation` improved from `5.167` to `4.965`
  - measured effect across the refinement pass: `soft_vacancy` improved from `4.453` to `4.371`
  - remaining conclusion: the biggest open regressions are now concentrated in low-volatility and stable-context slices, with `soft_vacancy` improved but still trailing `recent_blend`

Current source docs:

- [README.md](/root/WNBAStats/README.md)
- [TRAINING_SETUP.md](/root/WNBAStats/TRAINING_SETUP.md)

### Game Model

This includes:

- direct historical game models
- ATS residuals
- totals residuals
- curated game training DB
- matchup saved predictions

Pending work:

- split feature/promotion policy more explicitly by target:
  - spread / ATS
  - total
  - moneyline
- decide whether game residual training should stay anchored to saved prediction history or move further toward richer curated game-feature training
- improve total-pick calibration and decision quality, not just raw total regression error
- evaluate whether moneyline deserves a dedicated promotion path instead of being a side output
- add more game-evaluation slices:
  - close spreads
  - high totals
  - back-to-backs
  - injury-heavy slates
  - early-season windows
- decide whether to keep market anchors fixed or vary them by support/season
- add game-specific promotion gates similar to player markets instead of treating the whole game stack as one unit

Current source docs:

- [README.md](/root/WNBAStats/README.md)
- [TRAINING_SETUP.md](/root/WNBAStats/TRAINING_SETUP.md)

### Specials / Stocks

This includes:

- stocks snapshots
- prep pipeline
- calibration/thresholds
- board summaries

Pending work:

- promote fitted thresholds only when settled support is large enough
- decide final live threshold policy versus fallback defaults
- keep market-specific calibration conservative until settled history is materially larger
- continue performance work only where prep or read paths still show measurable waste
- keep candidate discovery and board ranking logic aligned with settled hit-rate behavior

Current source docs:

- [STOCKS_PLAN.md](/root/WNBAStats/STOCKS_PLAN.md)
- [README.md](/root/WNBAStats/README.md)

## Later Work

This section is for important but not immediate model/platform work.

## Shared Training / Artifact Work

These are cross-model requirements and should not be tracked inside only one model doc.

Pending work:

- make every promoted artifact carry explicit metadata:
  - training window
  - validation window
  - training rows
  - market/target
  - MAE/RMSE
  - directional accuracy
  - baseline deltas
  - calibration summary
  - git SHA
  - data snapshot hash
- version live feature schemas inside artifact bundles so inference can reject mismatches
- split artifact responsibilities cleanly:
  - regressor artifacts
  - calibration sidecars
  - threshold sidecars
  - feature-schema sidecars
  - evaluation sidecars
- port the stronger MLB-style promotion pattern into WNBAStats:
  - walk-forward-first promotion
  - per-market target policy
  - `component_only` / `blend` / `full_learned` decisions
- keep all new trainable model families on mounted-volume curated DB patterns only

Current source docs:

- [TRAINING_SETUP.md](/root/WNBAStats/TRAINING_SETUP.md)
- [README.md](/root/WNBAStats/README.md)

## Data / Evaluation Work

Pending work:

- keep the production training start at `2025-01-01`; the repaired 2024-2026 window slightly worsened final 2026 MAE and directional accuracy even though the residual layer improved
- evaluate a residual-only 2024 window and/or nonzero recency weighting before spending Odds API credits on 2023 props
- keep the completed 2024-2025 market-context repair healthy: all regular-season games and all prop games have spreads, while only preseason/All-Star rows remain intentionally unresolved
- re-audit final settled-prop counts after future historical imports; the `2026-08-02` audit confirmed `4,748/4,748` 2024 and `6,811/6,811` 2025 settlements have `team_spread`
- run a fresh settled-accuracy review on the current branch state
- decide whether to restore more of the deeper older evaluation/reporting tooling
- keep validating that curated signatures actually invalidate stale cached runs when feature/data shape changes

## Operational / Deployment Work

Pending work:

- keep frontend asset deployment reliable enough that manual volume syncs are not needed
- keep live cache invalidation and training-trigger paths aligned with the current mounted-volume runtime
- keep model docs updated when live promotion behavior changes, instead of letting README and model docs drift

## Immediate Priority Order

If work starts again right now, do it in this order:

1. Minutes-model bucket fixes.
2. Player-prop learned overlay promotion gates and `points` / combo-market diagnosis.
3. Game-model target separation and total-pick calibration.
4. Specials threshold promotion only after settled support justifies it.
5. Shared artifact-promotion and schema/versioning cleanup.

## Rules For Updating This File

When adding work here:

- put it under exactly one model family or one shared section
- describe the pending work, not the whole history
- move completed work out of this file instead of leaving checked boxes behind
- keep source-of-truth behavior in the specialized docs, not only here
