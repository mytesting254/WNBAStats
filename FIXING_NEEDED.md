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

- [x] Verify live frontend is serving the newest WNBA bundle after each deploy (current deployment is commit `e10e92f`).
- [x] Verify the recalculate button is hitting `POST /api/props/repair-current-slate` on live, not stale `/api/recalculate` (intended route is deployed and requires authentication).
- [x] Measure live current-slate repair behavior after the guarded repair restore: a no-line-change run exposed 831 unnecessary prediction rebuilds; current-slate repair now rebuilds affected games intentionally for injury/availability changes.
- [x] Confirm Nyara Sabally absence from the live matchup/parlay UI was expected while her market was out; verify weak-edge modeled props when an active sportsbook line is available.
- [x] Make `current_*` read caches and `app_response_cache_*.json` expire on local date rollover, not only TTL, so previous-day payloads cannot survive past midnight ET.
- [x] Remove the misleading unused `ODDSPAPI_KEY` deployment wiring so live Odds API imports only depend on `ODDS_API_KEY`.
- [x] Expose current live provider-env status (`ODDS_API_KEY` configured / missing) through the admin-only `/api/admin/provider-status` endpoint.
- [x] Make the nightly settlement workflow refresh a rolling seven-day ESPN results/box-score window before settlement, so completed games cannot remain scheduled after a missed overnight import.
- [x] Add an admin-only unsettled-prop audit and confirmed-DNP void action; DNP prop lines and dependent predictions/snapshots are deleted instead of being graded as zero.

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
- [x] Profile the cold Roster query; direct profiling found team injury-impact enrichment dominates the `7.9s` cold build, while the deployed cache-hit response is `43ms`.
- [x] Compress the 1.9 MB arena background image (WebP/AVIF and responsive variants); its network request is now deferred until after the Props view renders.

## Current Repair-Path Optimization Work

- [x] Rebuild only changed `prop_lines` during current-slate repair instead of rebuilding every scheduled prop in the touched games.
- [x] Add per-run caching for repeated `feature_snapshot()` inputs during rebuild.
- [x] Keep unchanged scheduled `prop_lines` in place during reingest instead of deleting and recreating every touched game row.
- [x] Apply Covers precedence before model-line sync so overlapping Odds API rows do not create duplicate live model rows for the same player market.
- [x] Scope roster-triggered background repairs to scheduled games involving affected teams; cached views remain available while the targeted repair runs.
- [x] Split injury-driven repairs from odds-driven repairs: Rotowire roster updates now skip sportsbook prop-line sync and go straight to affected-game player/game projection rebuilds, because injury changes alter usage and totals but do not require a fresh sportsbook ingest first.
- [x] Investigate duplicate player identities that may create unnecessary joins and duplicate prop rows (the only normalized-name collision is distinct Golden State/Japan Kokoro Tanaka records; no duplicate live prop-line keys found).
- [x] Keep Odds API cache rehydration scoped to active upcoming events instead of blindly reloading stale cached provider rows.
- [x] Tighten merged provider dedupe rules so overlapping Covers and Odds API rows keep distinct lines without over-inflating effectively identical offers.

## Current Model Quality Follow-Up

- [ ] Keep the component baseline as the live default until a revised learned layer beats it out-of-sample by market and season; `adaptive-context-v3-team-transition` scored MAE `2.802` versus its component baseline `2.638` on the mounted three-season DB. Tuning away extra recency weighting improved the candidate to `2.771`, but the latest live post-import server run on `2026-07-12` still lost to baseline overall (`2.771` vs `2.638`) and in every regular player-prop market, so it still must not be promoted.
- [x] Replace the universal residual blend with market-specific eligibility: verified in deployed `adaptive-context-v2-residual-guard`; it improved overall walk-forward MAE from `2.800` to `2.797`, keeps only the 200+ support `points_assists` experiment, and disables negative/sparse residual markets.
- [x] Add explicit post-transfer/team-change features: `adaptive-context-v3-team-transition` now includes games since joining, new-team minutes trend, teammate-minutes redistribution, and rotation stability. Its mounted three-season walk-forward result was worse overall (MAE `2.802` vs component `2.638`); the verified recent-transfer subset (`408` rows) was worse than the component baseline across all evaluated markets (points MAE `6.750` vs `4.524`). Retain it only as a candidate; do not enable a transfer-specific live adjustment.
- [x] Make recency weighting tunable in training/backtests. Mounted-DB walk-forward tuning found the prior `1.0x` scale (MAE `2.802`) and stronger `1.5x` scale (MAE `2.811`) worse than no extra recency weighting (MAE `2.771`), so the current candidate default is `0.0x`; retain `2024` for coverage without overweighting it.
- [x] Compare direct line-relative training (`actual_result - line`) against the current secondary residual blend per market. On the mounted run, only `points_assists` improved (MAE gain `0.614` across `203` rows); every other tested market was neutral or worse, so retain the existing `points_assists`-only residual gate and do not promote direct line-relative training end-to-end.
- [ ] Refit market-specific probability calibration and recommendation thresholds on the expanded `2024`–`2026` settled history, with minimum-support gates for sparse markets.
- [x] Prioritize settled prop history expansion before further model complexity work; historical Odds API event-player-prop imports now provide recovered runtime-cache coverage from `574` saved event payloads (`2024`: `263`, `2025`: `311`) plus active `2026` runtime prop history. A safety copy of the recovered payload cache now lives at `/root/WNBA_HISTORICAL_PLAYER_PROPS_CACHE`.
- [x] Add a repeatable settled-history gap audit/backfill workflow so missing settled prop dates can be expanded without manual date-by-date repair.
- [x] Make training-row quality explicit in `model_runs` / Model Lab: track per-market candidate rows, included rows, and exclusion reasons before changing model complexity further.
- [x] Import at least one additional full WNBA season of player/game history to improve early-season stability, rookie handling, and matchup/context coverage.
- [x] Upgrade evaluation from simple chronological 80/20 holdout to season-aware walk-forward backtests segmented by market and season/month window.
- [x] Add explicit baseline comparisons in Model Lab / reporting against `last_10_avg` and the component model so learned-model gains are measurable.
- [x] Separate model strategy by market depth: keep stronger learned/residual behavior for deeper markets and use more conservative shrinkage for sparse combo markets.
- [x] Focus the next feature pass on minutes and role-change prediction quality before trying more complex regressors.
- [ ] Finish the dedicated minutes-model improvement pass before more player-market complexity work. `2026-07-12` progress: the minutes layer is now materially safer for Specials and beats the old heuristic on the `2026` diagnostics window after team-transition features, guarded blend weights, explicit recency/baseline fallbacks, and a residual-on-`recent_blend` minutes architecture (`adaptive-context-v5-minutes-residual`). It is no longer blocking Specials work. Remaining gap: the final minutes path still trails the simple `recent_blend` baseline overall, so future minutes work should focus on better live-context features and direct evaluation of the exact production projection path rather than more baseline reshaping.
- [x] Add stricter row-quality gates after diagnostics land: ambiguous player/team identity, incomplete context, and weak historical windows should be excluded intentionally instead of blended silently.
- [ ] Review active-slate props after the residual-model rollout to see which markets still look too aggressive or too weak.
- [ ] Re-run before/after projection comparison after the archetype restore to quantify how many rows moved.
- [ ] Measure how often the residual model changes recommended side versus the raw stat model.
- [ ] Decide whether the residual blend weight should vary more aggressively by market.
- [ ] Split learned-model artifacts by responsibility: separate per-market regressor artifacts from calibration, recommendation-threshold, feature-schema, and evaluation sidecars so live promotion/debugging can isolate the failing layer quickly.
- [ ] Make every promoted model artifact carry explicit metadata: training window, validation window, training rows, market, MAE/RMSE, directional accuracy, baseline deltas, calibration summary, git SHA, and a data snapshot hash.
- [ ] Version the live feature schema inside each artifact bundle, including ordered feature names, default-fill behavior, and categorical handling, so inference can reject mismatched artifacts instead of silently projecting with drifted inputs.
- [ ] Add promotion gates for learned artifacts: minimum-support checks, per-market baseline wins, calibration sanity, and recent-window stability must pass before a new artifact can replace the current live component-backed default.
- [ ] Port the core MLBStats artifact-promotion pattern into WNBAStats: treat walk-forward summaries as the primary live-promotion gate, not just saved artifact metadata, and make the gate choose per market between `component_only`, `blend`, and `full_learned`.
- [ ] Replace the current all-or-nothing learned-layer behavior with target-specific overlay policy tables similar to MLBStats: `points`, `rebounds`, `assists`, combo markets, and Specials markets should each have explicit minimum support, baseline-delta, bias, and calibration checks before the learned layer can influence live output.
- [ ] Add minimum walk-forward segment / fold support to learned-player-market promotion so sparse short-window wins cannot promote a fragile market path.
- [ ] Add bias-based artifact checks (`mean_bias_abs`, worst-segment bias, or equivalent) before promotion; current underperformance is not only higher MAE/RMSE but also obvious directional drift in recent-transfer and early-window slices.
- [ ] Save artifact-level diagnostic slices by market/month/role/line bucket/minutes bucket/injury-adjusted slate so underperformance can be localized without rerunning full training analysis each time.
- [ ] Add explicit recent-transfer promotion policy: if the transfer slice underperforms the component baseline, keep that player-market on a component-backed fallback or reduced learned blend instead of applying the full learned adjustment.
- [ ] Add outlier-aware evaluation and training controls for WNBA player props, especially `points` and combo markets: ceiling-game misses should be measured explicitly, and training should support bounded/winsorized variants for comparison rather than assuming volatility features alone are enough.
- [x] Re-run full settled-prop holdout after role-aware minutes rollout: the refreshed `2024`–`2026` walk-forward run completed with `24,156` training rows; the current learned player layer remains behind the component baseline overall (MAE `2.800` vs `2.638`), so future tuning must beat this benchmark market by market.
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
- [ ] Reconfirm historical label coverage, especially whether `2025` settled props are still missing from the main runtime path. The live runtime import was still increasing on `2026-07-12` when checked manually (`2025` settled props had reached `5,521` mid-import), so re-audit final by-season settled counts after the cached import fully settles.
- [ ] Decide whether to restore more of the deeper evaluation/reporting tooling from the older branch.

## Infrastructure / Test Follow-Up

- [x] Repair stale `backend/tests/test_projection.py` expectations so the full file is clean again (`186 passed` on `2026-07-11`).
- [x] Finish the model-only `Special` steals/blocks tracker: snapshots now write to isolated `stocks_tracking.sqlite`, rebuild from scheduled players who already have live regular prop lines, settle from ESPN box-score steals/blocks, and stay separate from sportsbook EV/recommendation logic.
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
  - end-to-end `Special` tab support for model-only steals/blocks props, including backend list/generate endpoints, rebuild-time snapshot generation, settlement from ESPN box scores, and frontend review/generate UI
  - `Special` tab refinement: props now render in a rows-and-columns table with real matchup labels/tipoff times, and the last-five `stocks` outcomes plus minutes sit under the player name using the same inline recent-form strip style and color language as the regular props/parlay surfaces
  - active `2026-07-12` minutes-model workstream: document and test the minutes layer holistically, wire team-transition features (`games_since_joining_team`, new-team minutes trend, teammate-minutes redistribution, rotation stability) into minutes training/inference, and treat minutes diagnostics as a gating requirement before broader learned-model promotion
  - current minutes outcome on `2026-07-12`: live projection path is safer and no longer the obvious blocker for Specials, but `recent_blend` remains the strongest pure minutes baseline; the next minutes improvement should be richer live context, not another round of baseline math changes
  - `Special` tab now ranks per-game boards by `2+ stocks` probability instead of raw `STL+BLK`, shows matchup-level `50%+` candidate counts, and aligns the table ordering with the hit-rate / calibration stats already exposed in the UI
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
  - Active mounted-volume validation on `2026-07-11`: historical player props are settled across three seasons (`2024`: 381, `2025`: 600, `2026`: 5,986; `6,967` total).
  - Three-season walk-forward benchmark on `2026-07-11`: `24,156` training rows. Residual blending was negative for most markets; only `points_assists` showed a material residual MAE gain, pending confirmation with more settled rows.
  - Special-props validation on `2026-07-11`: local end-to-end implementation passed `188` backend tests and the frontend production build; mounted `stocks_tracking.sqlite` contained `8` projection snapshots with latest capture `2026-07-11T14:16:12.464186+00:00`.
  - Runtime-cache recovery on `2026-07-12`: the live WNBA volume still contained `574` historical Odds API player-prop payload files even though host `/data/cache` did not. The correct runtime cache path was `/var/lib/docker/volumes/pqsez6mkr14y0bnlhmcdmikg_wnba-data/_data/cache`, and the safety backup copy was saved to `/root/WNBA_HISTORICAL_PLAYER_PROPS_CACHE`.
  - Live post-import training check on `2026-07-12`: latest `model_runs.id=33` finished with `24,156` training rows and overall learned MAE `2.771` versus component baseline `2.638`. Regular player-prop learned markets all remained behind baseline; `points_assists` was still the only clear residual-layer winner, and game totals remained the strongest learned residual result.
  - Artifact-promotion review against `/root/MLBStats` on `2026-07-13`: the most relevant missing capability in WNBAStats is not a new estimator family but MLBStats-style runtime promotion policy. MLBStats trains artifacts freely, then uses walk-forward-first target policies to decide `replace`, `blend`, or fallback. WNBAStats should adopt the same pattern for per-market learned-player overlays.
  - Mounted WNBA player-prop artifact review on `2026-07-13`: current learned underperformance is broad, but concentrated in `points` and combo markets. The worst segment remains `2026-05` points, and the recent-transfer slice is materially worse than baseline (`points` transfer MAE `6.723` vs `4.514`). Residual overlays are still negative for most markets; only a few narrow cases remain candidates for gated promotion.
  - Cloned-live walk-forward check on `2026-07-13`: the newer `adaptive-context-v5-minutes-residual` stack still failed to beat the component player-prop baseline on a `/tmp` clone of the active runtime DB. Overall player-prop MAE was `2.766` versus baseline `2.629` and RMSE was `3.609` versus `3.453`; only `steals` posted even a marginal positive MAE delta (`+0.008`), while `points` and combo markets remained clearly regressive and the recent-transfer slice stayed sharply negative. Game residual evaluation remained a bright spot (`game_overall` MAE improvement `+3.110`, RMSE improvement `+3.796`), reinforcing that player-prop overlay promotion needs to stay conservative until the learned layer is materially stronger.
  - Injury-sync pipeline review on `2026-07-13`: the slow roster-refresh path was spending time in unnecessary sportsbook prop sync before rebuilding projections. The repair path now treats roster changes as a pure context update: identify affected scheduled games, rebuild full player props for those games, rebuild game predictions for those games, and then publish targeted caches. Odds/provider sync remains a separate path.
