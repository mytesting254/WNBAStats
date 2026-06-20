# FIXING NEEDED

## Current Restored Model Work

- [x] Restore WNBA game-total calibration.
- [x] Restore WNBA minutes model improvements.
- [x] Restore guarded current-slate repair flow.
- [x] Restore player archetype feature flags.
- [x] Restore top-center queue-clear toast.
- [x] Show all modeled matchup/parlay props, not only value-board-qualified rows.
- [x] Add role-aware minutes models with per-role fallback (core starter, starter volatile, rotation, bench, fringe).
- [x] Add date-cutoff protection for teammate contribution sampling in injury adjustments.
- [x] Add market-specific stabilization bands for noisier props.
- [x] Repair historical Covers matchup-line parsing for older visible-text-only matchup pages.
- [x] Allow historical Covers backfill to update `games.spread_home` / `games.game_total` even when no player prop rows exist.
- [x] Repair settled prop rows in place when historical context fields are incomplete.
- [x] Resolve settled prop game context from `player_team_history` for the specific game instead of only current roster state.
- [x] Fix player-model historical training rows so live context features are not hardcoded to neutral values.
- [x] Tighten settled-history probability calibration for player props with neighboring-bin support.
- [x] Add settled-history market bias correction to live player prop projections.
- [x] Add first-pass market-relative residual player model trained on `actual_result - line`.
- [x] Add first-pass learned game residual models for ATS margin and totals against saved market lines.

## Current Known Operational Follow-Up

- [ ] Verify live frontend is serving the newest WNBA bundle after each deploy.
- [ ] Verify the recalculate button is hitting `POST /api/props/repair-current-slate` on live, not stale `/api/recalculate`.
- [ ] Measure live current-slate repair timing after the guarded repair restore.
- [ ] Confirm Nyara Sabally rebounds and other weak-edge modeled props are visible in the live matchup/parlay UI after hard refresh.
- [ ] Make `current_*` read caches and `app_response_cache_*.json` expire on local date rollover, not only TTL, so previous-day payloads cannot survive past midnight ET.

## Current Repair-Path Optimization Work

- [x] Rebuild only changed `prop_lines` during current-slate repair instead of rebuilding every scheduled prop in the touched games.
- [ ] Add per-run caching for repeated `feature_snapshot()` inputs during rebuild.
- [ ] Batch current-slate rebuild work per game to reduce lock duration and improve recovery.
- [ ] Investigate duplicate player identities that may create unnecessary joins and duplicate prop rows.

## Current Model Quality Follow-Up

- [ ] Review active-slate props after the residual-model rollout to see which markets still look too aggressive or too weak.
- [ ] Re-run before/after projection comparison after the archetype restore to quantify how many rows moved.
- [ ] Measure how often the residual model changes recommended side versus the raw stat model.
- [ ] Decide whether the residual blend weight should vary more aggressively by market.
- [ ] Re-run full settled-prop holdout after role-aware minutes rollout (early minutes-only holdout delta: MAE `-0.074`, RMSE `-0.106`, bias `-0.034`).
- [x] Surface residual-model metrics in `model_runs` output so raw-stat and market-relative quality can be compared directly.
- [x] Surface game residual baseline-vs-blended deltas in Model Lab.
- [ ] Decide whether to train player props directly on line-relative targets end-to-end instead of using a secondary residual blend.
- [x] Add explicit evaluation/reporting for the new game residual layer so ATS and totals can be compared before/after blend on settled history.
- [ ] Decide whether game residual training should stay on saved prediction history or move to a richer game-feature training set once more settled game rows accumulate.

## Data / Evaluation Follow-Up

- [ ] Run a fresh WNBA settled-accuracy review on the current branch state.
- [x] Reconfirm repaired early-2026 matchup market coverage for settled props that were missing spread/total context.
- [ ] Reconfirm historical label coverage, especially whether `2025` settled props are still missing from the main runtime path.
- [ ] Decide whether to restore more of the deeper evaluation/reporting tooling from the older branch.

## Infrastructure / Test Follow-Up

- [ ] Decide whether to repair the unrelated baseline failures in `backend/tests/test_projection.py` so full-file runs are clean again.
- [ ] Keep frontend asset deployment reliable so manual volume syncs are not needed after every WNBA frontend change.

## Notes

- Current branch already includes:
  - game-total calibration
  - minutes model restore
  - guarded current-slate repair flow
  - player archetype features
  - matchup/parlay modeled-prop visibility fix
  - historical Covers visible-text matchup parser
  - repairable settled prop context
  - settled-history player probability calibration improvements
  - settled-history player projection bias adjustment
  - first-pass line-relative residual player model
  - first-pass ATS/total game residual model
  - game residual evaluation rows in `model_runs` (`game_ats`, `game_total`, `game_overall`)
- Nyara Sabally rebounds projection exists in the live DB and was being hidden by value-board gating, not missing from the model pipeline.
