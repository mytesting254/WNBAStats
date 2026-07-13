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
- candidate eligibility now uses the last `10` games instead of `8`
- the fast prep estimator uses a `10`-game recent window with a `20`-game stabilizing window
- the fast prep estimator blends same-home/away history and scheduled-game `rest_days` into the component baseline without re-enabling the expensive learned/minutes path
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
- `default_prep_dates(include_tomorrow=True)`

These are enough for:

- manual prep runs
- cron-driven prep jobs
- targeted reruns by date or game

## Invalidation Rules

Rebuild affected game entries in `stocks_tracking.sqlite` when:

- a player injury / availability changes
- a scheduled game changes status or date
- team/player identity context changes materially

Do not rerun the full slate unless the schedule itself changed broadly.

## Remaining Steps

1. Add `player_prep_features` materialization in `stocks_tracking.sqlite` so projection context is computed once per scheduled prep run.
2. Make `snapshot_stocks()` prefer cached prep features and fall back to on-demand context rebuilding only when needed.
3. Add optional prep metadata / job-run tables if we want observability in `stocks_tracking.sqlite`.
4. Wire roster-change invalidation to targeted stocks refreshes by affected game.
5. Decide and test the optimal historical windows for candidate eligibility and stocks summaries.
6. Expand tomorrow-prep outputs if we want extra precomputed summaries for the UI.

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
