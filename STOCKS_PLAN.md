# Stocks Plan

## Goal

Move the `Special` stocks pipeline from a narrow on-demand snapshot flow to a prepared, cron-driven workflow that:

- captures more viable players than the current `prop_lines`-only filter
- keeps heavy candidate discovery out of the live request path
- isolates richer derived data in `stocks_tracking.sqlite`
- leaves game-day work focused on roster and odds deltas

## Current State

Today the stocks generator:

- reads scheduled candidates from `prop_lines`
- excludes clearly unavailable players
- calls `predict_player_prop()` for `steals`, `blocks`, and `blocks_steals`
- writes snapshots into `stocks_tracking.sqlite`
- serves the `Special` tab from the latest snapshots

This means:

- role/history are used inside the projections
- but only players who already have regular prop coverage enter the candidate pool
- potentially useful stocks players are missed if they have no regular `prop_lines`

## Implemented Now

The repo now has the first prep-oriented Stocks layer:

- `prepared_games` table in `stocks_tracking.sqlite` copied from canonical scheduled `games`
- `candidate_players` table in `stocks_tracking.sqlite`
- candidate generation from scheduled teams plus recent minutes / recent stocks history
- `snapshot_stocks()` can read from the candidate pool instead of depending only on `prop_lines`
- `prepare_stocks_data()` can prepare selected slate dates in one call
- `scripts/prepare_stocks_data.py` and `scripts/live_stocks_prep.sh` provide cron-friendly prep entry points
- `deploy/wnba-stocks-prep.cron` documents the host-cron install pattern for 11pm ET tomorrow prep

Current implementation notes:

- canonical reads still come from `wnba.sqlite`
- selected scheduled games are copied into `prepared_games` first, then downstream prep reads the internal slate
- derived prep/output stays in `stocks_tracking.sqlite`
- default prep dates are today + tomorrow in `America/New_York`
- the initial production schedule is 11:00 PM `America/New_York` to prepare tomorrow's slate on the night before
- candidate eligibility now uses the last `12` games instead of the earlier `10`
- the fast prep estimator now uses a `12`-game recent window with a `24`-game stabilizing window
- the fast prep estimator blends same-home/away history and scheduled-game `rest_days` into the component baseline without re-enabling the expensive learned/minutes path
- scheduled prep now materializes `player_prep_features` per game/player/market so snapshot generation can reuse cached contextual projections and recent `2+ stocks` hit-rate inputs
- `player_prep_features` now also persists projected minutes, minute volatility, and injury/opportunity-side prep context so future calibration work can train against those fields without re-querying the live runtime
- scheduled prep now materializes `team_prep_context` per game/team so all candidates on one side share the same cached pace, opponent-allowed, and turnover-pressure context
- scheduled prep now materializes `game_board_summaries` per game so matchup-level counts and top-board metrics are precomputed for tomorrow reads
- the Specials read path now batches active-prop, matchup-context, and recent-history lookups instead of issuing per-player N+1 queries
- the Specials tab now consumes precomputed `game_board_summaries` for matchup-level counts and board averages instead of deriving those from raw snapshot rows in the UI
- snapshot prep now skips unchanged `(game_id, player_id)` writes by comparing against the latest cached row before inserting a new snapshot
- snapshot rows now persist durable candidate, prep, injury/opportunity, and matchup-context fields directly on `projection_snapshots` so later settled audits do not break when mutable prep cache tables are rebuilt
- `scripts/backfill_stocks_snapshot_context.py` can reconstruct and backfill those durable snapshot fields for older historical rows
- Special-tracking `game_date` should stay aligned to the canonical slate date from `games.game_date` (ET slate date), even when late games cross midnight in UTC
- specials calibration now uses smaller neighborhood windows plus explicit minimum total-history floors before any empirical blend can activate
- historical prep/projection inputs now downweight blowout rows by role and winsorize per-market tails before recent/stability anchors are blended
- starter-heavy prep/projection paths now also drop only `20+` margin blowout rows once at least `5` competitive games remain, so stars keep enough sample while late-game noise stops inflating anchors
- Specials stats now expose support-gated threshold-fit scaffolding for `high` and `watch` cutoffs from settled `2+ stocks` snapshot history, while falling back to `55% / 45%` defaults when the sample is too thin
- the Specials board and prep summaries now use the fitted `high` threshold for candidate counts and hit-rate display when support is sufficient, instead of hard-coded `50%+` labeling
- stale pending Special rows for final games are now pruned when no matching `player_game_stats` row exists and there is no active prop line for that player/game, which prevents permanently pending late-slate bench rows
- the `Special Props` table now shows an `H2H STK` strip with prior `blocks_steals` results and minutes versus the current opponent
- live mounted-runtime validation completed end to end in about `1m 1s` for a `3`-game / `79`-candidate ET-today slate

## Current Optimization Track

The next projections optimization should keep the same model behavior but reduce repeated prep work:

- materialize per-player prep features into `stocks_tracking.sqlite`
- compute those features once during the nightly / scheduled prep run
- let snapshot generation read cached prep features instead of re-querying historical context for every rerun
- keep a fallback path so snapshots can still rebuild on demand if cached prep features are missing

Planned cached prep fields:

- base component projection per market (`steals`, `blocks`, `blocks_steals`)
- contextualized projection after venue and `rest_days` blending
- recent and stabilizing history anchors
- same-home/away sample counts
- recent `2+ stocks` hit-rate blend inputs

Expected impact:

- faster reruns for today-only targeted refreshes
- less repeated scanning of `player_game_stats` from the main DB
- more predictable cron runtime as the candidate pool grows
- cleaner separation between canonical history reads and derived Stocks prep outputs

## Next Four Optimization Tracks

These are the next four high-value Stocks improvements to implement together after the current prep foundation:

1. `player_prep_features` materialization
2. opponent/team-context caching for Specials
3. richer candidate inclusion logic
4. market-specific probability calibration for `stocks 2+` / `stocks 3+`

### 1. `player_prep_features` Materialization

Goal:

- compute reusable player-level Stocks prep features once during scheduled prep
- persist them in `stocks_tracking.sqlite`
- let `snapshot_stocks()` reuse cached feature rows instead of rebuilding the same historical context repeatedly

Planned contents:

- per-game, per-player, per-market base component projection
- contextualized projection after home/away and `rest_days` blending
- recent-window and stabilizing-window anchors
- same-venue sample counts and venue-adjusted rates
- recent `2+ stocks` hit-rate inputs

Expected win:

- faster reruns
- less pressure on `wnba.sqlite`
- more stable cron runtime as candidate counts increase

Current status:

- implemented
- `prepare_stocks_data()` now builds `player_prep_features` before `snapshot_stocks()`
- `snapshot_stocks()` prefers cached prep rows and falls back to on-demand feature generation only when the cache is missing

### 2. Opponent And Team-Context Caching

Goal:

- add more matchup-specific defensive context without making live regeneration expensive

Planned context to precompute per game/team:

- opponent stocks allowed tendencies
- turnover-pressure indicators
- pace / possession context
- rim-protection and block environment
- role/archetype-sensitive opponent allowances
- injury-driven opportunity or suppression context at the team level

Expected win:

- better Specials ranking quality
- better differentiation for defensive specialists whose value depends more on matchup than raw recent average
- reusable game-level context shared across every candidate in the same matchup

Current status:

- implemented
- scheduled prep now builds `team_prep_context` rows per game/team
- cached context currently includes pace, opponent steals allowed, opponent blocks allowed, combined stocks allowed, and turnover-pressure factors
- `player_prep_features` now consumes that cached matchup context, and snapshot fallback paths reuse it when available
- `player_prep_features` now persists projected minutes, minute volatility, injury availability/usage deltas, and opportunity context as prep-only fields
- the August 6, 2026 full-history backtest kept those richer player fields out of the live scoring multiplier because recent-hit-rate gains came with worse probability calibration over the broader settled sample

### 3. Richer Candidate Inclusion Logic

Goal:

- widen candidate discovery without flooding the live board with low-value fringe names

Planned additions:

- role-based inclusion for defensive specialists even when minutes are modest
- lineup-promotion detection
- injury-opportunity replacement detection
- opponent-sensitive boosts for players who profile well against a specific matchup
- overnight long-tail candidate scoring with stricter UI filtering at serve time

Expected win:

- fewer missed Stocks candidates
- better coverage in games where value comes from role change rather than season-long baseline usage
- better use of the ahead-of-time cron path instead of live discovery

Current status:

- implemented
- candidate selection now has explicit inclusion paths for defensive-specialist profiles, recent lineup promotions, and injury-opportunity replacements
- the prep query now uses recent-12, recent-3, and stabilizing-24 windows instead of only one recent-minutes/stocks threshold
- candidate reasons now distinguish stable rotation, recent promotion, defensive specialist profile, and injury replacement context

### 4. Market-Specific Probability Calibration

Goal:

- stop treating `steals`, `blocks`, `stocks 2+`, and `stocks 3+` as if they share the same probability shape

Planned calibration work:

- separate blend weights by market
- separate support thresholds by market
- use history/calibration differently for `stocks 2+` versus `stocks 3+`
- explicitly test sparse-market stability before promotion
- preserve conservative behavior for long-tail `3+` outputs

Expected win:

- cleaner ordering of live Specials cards
- fewer inflated `3+` outputs
- better hit-rate alignment between displayed probability and real outcome frequency

Current status:

- implemented
- settled-history calibration is now split by market and threshold instead of only applying to `2+ stocks`
- `steals`, `blocks`, `2+ stocks`, and `3+ stocks` each now use their own support thresholds, sample limits, and blend weights
- the player-history blend remains specific to `2+ stocks`, while long-tail `3+` outputs stay more conservative

## Quality Controls

The four-track optimization pass now keeps these safeguards:

- no promotion of learned player-prop overlays unless they beat the component baseline
- cached prep features must have an on-demand fallback path
- wide candidate discovery should happen in prep, not in user-facing requests
- `stocks 3+` stays conservative and support-aware
- matchup boosts should be bounded so single noisy opponent splits do not overtake the baseline

## Target Architecture

Use `wnba.sqlite` as the canonical source of truth and `stocks_tracking.sqlite` as the derived-work database.

### Canonical Inputs In `wnba.sqlite`

- scheduled games
- teams and players
- injury statuses
- player game history
- player team history
- regular prop lines

### Derived Outputs In `stocks_tracking.sqlite`

- prepared games for the selected slate
- candidate players per scheduled game
- candidate build metadata
- prep run metadata
- game-level board summaries
- recent stocks summaries per player
- prepared specials snapshots for today and tomorrow
- job timestamps / invalidation metadata

## Pipeline Split

### Ahead-Of-Time Prep

Run on a cron schedule.

Responsibilities:

- identify upcoming scheduled games
- build candidate pools per game
- materialize reusable player prep features per game/player/market
- record prep-run scope, counts, and timestamps for observability
- compute recent stocks summaries from history
- generate initial stocks snapshots
- prepare tomorrow/today data before users request it

This path can afford richer scanning because it is not user-facing latency.

Recommended initial schedule:

- `11:00 PM America/New_York` prepare tomorrow's data
- morning refresh in `America/New_York` for today/tomorrow
- intraday refreshes for today's slate only

### Game-Day Reactive Updates

Run only when context changes.

Responsibilities:

- roster / injury changes
- sportsbook line changes if needed for related views
- targeted regeneration for affected games only, preferably from cached prep features when the underlying slate has already been prepared

For stocks specifically, roster changes should be the main invalidation trigger.

## Candidate Strategy

Do not rely only on `prop_lines`.

Candidate pool should come from scheduled games plus filtered player eligibility.

Recommended filters:

- player belongs to one of the two scheduled teams
- player is not `out` / inactive / suspended / unavailable
- player has recent game history
- player has enough recent minutes to be a plausible rotation candidate
- optional tighter filters for likely starters / rotation players

This should broaden coverage without scanning full rosters blindly on every run.

Current implementation uses:

- scheduled games only
- no hard-out / inactive / suspended / unavailable players
- recent history required
- recent minutes / recent stocks thresholds over the last `10` games to keep the pool rotation-focused
- a fast component-only Stocks estimator with:
  - recent-form emphasis from the last `10` games
  - stability anchor from up to the last `20` games
  - home/away venue-specific adjustment when enough same-venue history exists
  - scheduled-game `rest_days` adjustment

## Why Use `stocks_tracking.sqlite`

Reasons to keep this work out of the main runtime DB:

- reduce write pressure on `wnba.sqlite`
- reduce lock contention with normal app sync / rebuild paths
- allow richer derived tables for Specials without polluting the main schema
- make stocks prep independently rebuildable

The dependency flow should stay one-way:

- read canonical state from `wnba.sqlite`
- copy the selected upcoming slate into `prepared_games`
- write derived stocks prep/results to `stocks_tracking.sqlite`

## Cron Plan

### Night-Before Prep

- build tomorrow candidate pools
- compute recent summaries
- generate tomorrow baseline snapshots
- install via `deploy/wnba-stocks-prep.cron`
- command: `WNBA_USE_LIVE_CONTAINER=true scripts/live_stocks_prep.sh tomorrow`

### Morning Refresh

- refresh candidate pools for today
- regenerate today snapshots with latest known context
- same wrapper can run `scripts/live_stocks_prep.sh today`

### Intraday Refresh

- every 15 to 30 minutes during active slate windows
- refresh only today’s scheduled games
- on roster changes, rerun only affected games
- same wrapper can run `scripts/live_stocks_prep.sh dates YYYY-MM-DD`

## Implemented Functions

Current code paths:

- `rebuild_prepared_games(conn, game_ids=None, target_dates=None)`
- `rebuild_candidate_players(conn, game_ids=None, target_dates=None)`
- `snapshot_stocks(conn, game_ids=None, target_dates=None, runtime_cache=None)`
- `prepare_stocks_data(conn, target_dates=None)`
- `prepare_stocks_data(conn, target_dates=None, game_ids=None)`
- `default_prep_dates(include_tomorrow=True)`
- `queue_prepare_stocks_games(game_ids=None)`
- `rebuild_game_board_summaries(game_ids=None, target_dates=None)`

These are enough for:

- manual prep runs
- cron-driven prep jobs
- targeted reruns by date or game

## Invalidation Rules

Rebuild affected game entries in `stocks_tracking.sqlite` when:

- a player injury / availability changes
- a scheduled game changes status or date
- team/player identity context changes materially

Current status:

- roster/injury-triggered refresh now supports targeted game invalidation
- the Rotowire injury import path queues a game-scoped Stocks prep refresh for the affected scheduled games
- game-scoped prep reuses the same prep pipeline, but only for selected `game_id` values
- prep runs are now recorded in `prep_runs` with scope, selected dates/games, counts, status, and timestamps
- matchup-level board summaries are now recorded in `game_board_summaries` with player counts, `50%+` counts, average probabilities, and top-board metrics

Do not rerun the full slate unless the schedule itself changed broadly.

## Remaining Steps

1. Re-evaluate the tuned `12/24` Stocks windows against live hit-rate and coverage after more settled rows accumulate.
2. Re-evaluate whether the fitted threshold should become the new canonical prep default once the live settled sample is reliably off the fallback defaults.
3. Re-evaluate the current extreme-blowout prune thresholds (`20+` margin, `5` competitive rows) once enough settled live history exists to compare starter/star quality before and after the filter.

## Non-Goals

- Do not move core canonical roster/game/player truth into `stocks_tracking.sqlite`.
- Do not make request-time UI reads do broad candidate discovery.
- Do not widen the live on-demand generation path before the scheduled prep path exists.

## Success Criteria

- more stocks candidates than the current `prop_lines`-only approach
- no noticeable slowdown in the user-facing `Special` tab
- reduced dependency on live request-time discovery
- isolated derived-data growth in `stocks_tracking.sqlite`
- roster changes trigger targeted stocks refresh instead of full-slate recompute
