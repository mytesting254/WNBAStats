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
- [x] Remove the misleading unused `ODDSPAPI_KEY` deployment wiring so live Odds API imports only depend on `ODDS_API_KEY`.
- [ ] Decide whether to expose current live provider-env status (`ODDS_API_KEY` present / missing) in an admin/debug surface.

## Initial App-Load Performance

- [x] Stop blocking the first render on every dashboard payload: load the value board and auth state first, then render the Props tab as soon as the board is available.
- [x] Lazy-load tab-specific data (Gems, Watchlist, Matchups, Parlays, Discrepancies, Roster, and Models) only when the user opens that tab.
- [x] Split the single global dashboard `loading` state into per-view loading states so one slow optional endpoint cannot hold the visible Props view hostage.
- [x] Treat cached read payloads as stale-while-revalidate: return the latest valid payload immediately and rebuild it asynchronously after source data changes instead of forcing a synchronous recompute after the five-minute TTL.
- [x] Replace the fixed 300-second read-cache refresh policy with mutation-driven invalidation/rebuilds for affected views; add a single-flight guard so concurrent visitors cannot stampede a cold cache.
- [x] Prewarm Value Board, Matchups, and Roster after odds imports, injury refreshes, current-slate repairs, and model training complete; do not make the next visitor rebuild those payloads.
- [x] Prefetch Matchups and Roster during browser idle time after the Value Board has rendered, without delaying the initial Props view.
- [x] Make the app-response cache a real read-through cache on normal GET requests, rather than writing each response and reading it only as a SQLite-lock fallback.
- [x] Add a lightweight cache-version/event channel (SQLite event/version table initially; Redis pub/sub if scaled out) so imports, repairs, and model runs trigger targeted cache rebuilds and browsers refresh only affected views.
- [x] Use SSE or WebSocket notifications to tell open browsers that a specific view changed; remove broad dashboard-data polling and refresh only the affected visible view.
- [x] Add conditional API responses (ETag/`If-None-Match` or version checks) for unchanged view payloads.
- [x] Report an unambiguous cache outcome (`HIT`, `STALE`, `MISS`, `REBUILDING`) and compute duration in response headers; current `X-App-Cache: STORE` is written for every successful response and cannot distinguish cache hits.
- [x] Profile the cold Matchups query and remove repeated per-game injury-impact queries by sharing one slate-level game-prediction cache; direct profiling found `11.2s` of a `13.8s` build in repeated game prediction/injury work.
- [ ] Profile the cold Roster query; first measured response was `7.7s`, versus warm `12–21ms`.
- [ ] Compress the 1.9 MB arena background image (WebP/AVIF and responsive variants); its network request is now deferred until after the Props view renders.

## Current Repair-Path Optimization Work

- [x] Rebuild only changed `prop_lines` during current-slate repair instead of rebuilding every scheduled prop in the touched games.
- [x] Add per-run caching for repeated `feature_snapshot()` inputs during rebuild.
- [x] Keep unchanged scheduled `prop_lines` in place during reingest instead of deleting and recreating every touched game row.
- [x] Apply Covers precedence before model-line sync so overlapping Odds API rows do not create duplicate live model rows for the same player market.
- [ ] Batch current-slate rebuild work per game to reduce lock duration and improve recovery.
- [ ] Investigate duplicate player identities that may create unnecessary joins and duplicate prop rows.
- [x] Keep Odds API cache rehydration scoped to active upcoming events instead of blindly reloading stale cached provider rows.
- [x] Tighten merged provider dedupe rules so overlapping Covers and Odds API rows keep distinct lines without over-inflating effectively identical offers.

## Current Model Quality Follow-Up

- [ ] Prioritize settled prop history expansion before further model complexity work; current residual markets are still thin (`points 354`, `rebounds 299`, `threes 204`, `assists 67`, `PRA 71`, `PR 37`, `PA 34`, `RA 26`).
- [x] Add a repeatable settled-history gap audit/backfill workflow so missing settled prop dates can be expanded without manual date-by-date repair.
- [ ] Make training-row quality explicit in `model_runs` / Model Lab: track per-market candidate rows, included rows, and exclusion reasons before changing model complexity further.
- [x] Import at least one additional full WNBA season of player/game history to improve early-season stability, rookie handling, and matchup/context coverage.
- [x] Upgrade evaluation from simple chronological 80/20 holdout to season-aware walk-forward backtests segmented by market and season/month window.
- [x] Add explicit baseline comparisons in Model Lab / reporting against `last_10_avg` and the component model so learned-model gains are measurable.
- [x] Separate model strategy by market depth: keep stronger learned/residual behavior for deeper markets and use more conservative shrinkage for sparse combo markets.
- [x] Focus the next feature pass on minutes and role-change prediction quality before trying more complex regressors.
- [x] Add stricter row-quality gates after diagnostics land: ambiguous player/team identity, incomplete context, and weak historical windows should be excluded intentionally instead of blended silently.
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

- [x] Backfill the main runtime DB with the currently available deploy-history window (`2025-05-02` through `2026-06-04`) so local evaluation does not depend on the deploy snapshot file alone.
- [ ] Confirm whether older pre-2025 seasons can be imported cleanly enough to support one-more-season model training without breaking team/player identity joins.
- [ ] Run a fresh WNBA settled-accuracy review on the current branch state.
- [x] Reconfirm repaired early-2026 matchup market coverage for settled props that were missing spread/total context.
- [ ] Reconfirm historical label coverage, especially whether `2025` settled props are still missing from the main runtime path.
- [ ] Decide whether to restore more of the deeper evaluation/reporting tooling from the older branch.

## Infrastructure / Test Follow-Up

- [ ] Decide whether to repair the unrelated baseline failures in `backend/tests/test_projection.py` so full-file runs are clean again.
- [ ] Keep frontend asset deployment reliable so manual volume syncs are not needed after every WNBA frontend change.
- [x] Make unchanged local training reruns reuse the latest matching `model_runs` result instead of recomputing the same walk-forward benchmark.

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
  - live WNBA deployment/docs cleanup so The Odds API path uses `ODDS_API_KEY` only
  - active-date filtering when replaying cached Odds API event payloads
  - combined Covers + Odds API raw row inventory in `sportsbook_prop_lines` with Covers-first sync precedence at the final `prop_lines` layer
  - segmented walk-forward player-prop evaluation saved in `model_runs` with per-market baseline deltas and segment counts
  - Model Lab display for baseline-vs-model deltas and evaluation segment coverage
  - connection-scoped historical training caches for player rows, archetypes, matchup factors, and context reuse
  - run-scoped shared player/game projection context so repeated same-player market rebuilds reuse minutes/context setup
  - diff-based prop-line reingest so unchanged rows and predictions are preserved across current-slate repairs
  - parallel per-market training evaluation in `run_walk_forward_training()`
  - signature-based reuse of unchanged `model_runs` so repeat local training returns instantly when the data has not changed
- Current data state checked on `2026-06-26`:
  - local `data/wnba.sqlite` now mirrors the deploy snapshot and includes imported `2024` ESPN history
  - active local history spans `2024-05-03` through `2026-09-24`
  - `686` final games plus `259` scheduled future games
  - `player_game_stats`: `13,196` rows across `435` players
  - `settled_props`: `1,092` rows
  - latest saved segmented walk-forward learned-model run used `123,750` evaluation rows total
  - latest saved local walk-forward run (`2026-06-26T17:03:18Z`) finished in about `1m 41s`
  - unchanged repeat training call on the same DB state returned the cached run in about `0.65s`
- Nyara Sabally rebounds projection exists in the live DB and was being hidden by value-board gating, not missing from the model pipeline.
