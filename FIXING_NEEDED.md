# Model Fixing Needed

This is the consolidated model-work index for the repo.

Use this document to separate:

- active model fixes
- training/data-pipeline work
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
- split artifact promotion by market so weak learned markets stay `component_only` while stronger ones can blend
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
  - `core_starter`
  - `2026` slice
- keep cleanup/calibration focused on hard buckets rather than broad feature expansion
- confirm whether current row-quality cleanup is enough or whether more role-specific pruning is needed
- document final promotion criteria for minutes so it can gate downstream player-prop work cleanly

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

- confirm whether older pre-2025 seasons can be imported cleanly enough for safe training use
- re-audit final settled-prop counts by season after all historical imports settle
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
