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

## Current Known Operational Follow-Up

- [ ] Verify live frontend is serving the newest WNBA bundle after each deploy.
- [ ] Verify the recalculate button is hitting `POST /api/props/repair-current-slate` on live, not stale `/api/recalculate`.
- [ ] Measure live current-slate repair timing after the guarded repair restore.
- [ ] Confirm Nyara Sabally rebounds and other weak-edge modeled props are visible in the live matchup/parlay UI after hard refresh.

## Current Repair-Path Optimization Work

- [x] Rebuild only changed `prop_lines` during current-slate repair instead of rebuilding every scheduled prop in the touched games.
- [ ] Add per-run caching for repeated `feature_snapshot()` inputs during rebuild.
- [ ] Batch current-slate rebuild work per game to reduce lock duration and improve recovery.
- [ ] Investigate duplicate player identities that may create unnecessary joins and duplicate prop rows.

## Current Model Quality Follow-Up

- [ ] Review active-slate props after restoring minutes, total calibration, and archetypes to see whether outputs still look too aggressive or too weak.
- [ ] Re-run before/after projection comparison after the archetype restore to quantify how many rows moved.
- [ ] Check whether low-edge side flips are acceptable or whether probability calibration still needs tightening.
- [ ] Re-run full settled-prop holdout after role-aware minutes rollout (early minutes-only holdout delta: MAE `-0.074`, RMSE `-0.106`, bias `-0.034`).

## Data / Evaluation Follow-Up

- [ ] Run a fresh WNBA settled-accuracy review on the current branch state.
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
- Nyara Sabally rebounds projection exists in the live DB and was being hidden by value-board gating, not missing from the model pipeline.
