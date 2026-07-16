# Ratings Plan

## Goal

Add team offensive, defensive, and net ratings to the app in a way that is:

- fast to compute from existing data
- safe for model training without future leakage
- useful in both UI and prediction models

This plan assumes we build the ratings internally from existing box-score-derived team results instead of depending on a third-party rankings feed.

## Why Build It Ourselves

The repo already stores the inputs needed for team efficiency ratings:

- `team_game_results.points`
- `team_game_results.opponent_points`
- `team_game_results.possessions`

That makes first-party ratings straightforward:

- `off_rating = 100 * points / possessions`
- `def_rating = 100 * opponent_points / possessions`
- `net_rating = off_rating - def_rating`

Owning the computation is better than depending on an external rank because:

- definitions stay consistent across UI and models
- ratings can be recomputed for any historical cutoff date
- we can create rolling and split contexts that are model-safe

## Existing Data Source

Primary table:

- [backend/app/db.py](/root/WNBAStats/backend/app/db.py:899) defines `team_game_results`

Relevant fields:

- `team_id`
- `game_id`
- `is_home`
- `points`
- `opponent_points`
- `possessions`

This is sufficient for first-pass team ratings.

## Metrics To Add

Core metrics:

- offensive rating
- defensive rating
- net rating

Supporting metrics:

- pace
- offensive rank
- defensive rank
- net rank

Optional later:

- effective FG%
- turnover rate
- offensive rebound rate
- free throw rate
- opponent versions of the same
- four-factor-based composite context

## Rating Windows

We should not rely on a single window.

Recommended windows:

- `season_to_date`: stable baseline
- `last_10`: current form
- `last_5`: optional, high-volatility trend signal
- `home_only`
- `away_only`

Recommended product behavior:

- UI should emphasize `season_to_date` and `last_10`
- `home_only` and `away_only` should be shown as context, not as the main rating
- `last_5` should be optional and secondary because it is noisy

## Recommended Blend

For predictive usage, do not choose one window only.

Good first blend:

- `60% season_to_date`
- `25% last_10`
- `15% venue_split`

Where:

- use `home_only` for the home team
- use `away_only` for the away team

If sample sizes are too small:

- shrink `home_only` or `away_only` toward season average
- shrink `last_5` heavily or exclude it

## Sample Size Rules

To avoid noisy outputs:

- do not trust `last_5` without enough possessions
- do not trust `home_only` or `away_only` if the split sample is too small
- fall back toward `season_to_date` when sample is weak

Suggested shrink rules:

- if split sample `< 5 games`, heavily shrink to season
- if rolling sample `< 5 games`, fall back to broader window
- if possessions are missing or obviously stale, suppress rank display

## UI Placement

### 1. Matchups View

Primary location.

Current matchup cards already present a natural place for team context:

- [frontend/src/App.tsx](/root/WNBAStats/frontend/src/App.tsx:3328)
- [frontend/src/api.ts](/root/WNBAStats/frontend/src/api.ts:625)

Recommended display per team:

- `Off Rtg`
- `Def Rtg`
- `Net`
- league rank

Recommended display at matchup level:

- net differential
- offense rank comparison
- defense rank comparison

Example:

- `Net: +6.1 vs -1.8`
- `Off Rank: 2 vs 9`
- `Def Rank: 4 vs 10`

### 2. Value Board

Secondary location.

Useful as opponent context on player props:

- `vs No. 3 defense`
- `opp def rtg 97.8`
- `pace-up matchup`

This is especially useful for:

- points props
- assists props
- combo props

### 3. Roster View

Lower priority.

Do not attach team ratings to each player row.

If shown there, keep it in team summary only:

- team offensive rating
- team defensive rating
- team net rating

## Backend Plan

### Phase 1: Add a Ratings Compute Layer

Create a reusable function that computes ratings from `team_game_results`.

Outputs per team:

- games
- possessions
- points per 100 possessions
- opponent points per 100 possessions
- net rating
- pace

Outputs by window:

- season
- last 10
- last 5
- home
- away

### Phase 2: Expose Ratings in Matchup Payloads

Inject ratings into matchup payloads in:

- [backend/app/main.py](/root/WNBAStats/backend/app/main.py:3264)

Recommended payload additions:

- `home_team_ratings`
- `away_team_ratings`
- `rating_differentials`

Suggested structure:

```json
{
  "home_team_ratings": {
    "season": {"off": 103.4, "def": 98.9, "net": 4.5, "pace": 79.2, "off_rank": 3, "def_rank": 2, "net_rank": 2},
    "last_10": {"off": 101.8, "def": 97.4, "net": 4.4, "pace": 78.8},
    "home": {"off": 104.1, "def": 97.9, "net": 6.2}
  }
}
```

### Phase 3: Add API Type Support

Extend:

- [frontend/src/api.ts](/root/WNBAStats/frontend/src/api.ts:625)

### Phase 4: Render in Matchups UI

Use the existing team summary cards.

Keep the first version compact and readable.

## Model Enrichment Plan

## Game Model

This is high-value for game predictions.

Add features such as:

- home offensive rating
- home defensive rating
- away offensive rating
- away defensive rating
- home net rating
- away net rating
- offensive rating differential
- defensive rating differential
- net rating differential
- pace differential
- last-10 offensive/defensive/net differentials
- home/away split adjustments

These should fit naturally into:

- [backend/app/game_predictions.py](/root/WNBAStats/backend/app/game_predictions.py)

## Prop Model

Also useful, especially as opponent/team context.

Examples:

- opponent defensive rating
- opponent last-10 defensive rating
- player team offensive rating
- expected pace context
- venue-specific rating context

These belong in:

- [backend/app/player_prop_model.py](/root/WNBAStats/backend/app/player_prop_model.py)

## Leakage Rules

This is the most important modeling rule.

Historical training rows must use ratings computed only from games before the target game date.

Never use:

- end-of-season ratings for earlier historical games
- ratings that include the target game itself
- ratings refreshed from future games relative to the training row

For training:

- compute ratings as-of each game date
- use only prior games in the same season or configured history window

For live inference:

- compute ratings from completed games only

## Suggested Feature Priority

Highest priority:

- season offensive rating
- season defensive rating
- season net rating
- last-10 net rating
- pace

Second priority:

- home split net rating
- away split net rating
- opponent last-10 defensive rating
- offensive/defensive rating differentials

Third priority:

- last-5 features
- four-factor components
- opponent-strength-adjusted ratings

## Recommended First Implementation

Start small:

1. Compute season, last-10, home, and away ratings from `team_game_results`
2. Inject them into matchup payloads
3. Display them in Matchups UI
4. Add season and last-10 differentials to the game prediction model

That gets immediate value with limited complexity.

## Recommended Second Implementation

After the first pass is stable:

1. Add pace and four-factor context
2. Add opponent defensive rating features to prop models
3. Add historical as-of-date rating generation for training
4. Add a dedicated `/api/team-ratings` endpoint if needed

## Open Decisions

Still to decide before implementation:

- whether to persist ratings in a dedicated table or compute on read
- whether to cache a materialized ratings payload
- exact shrinkage formula for small-sample home/away splits
- whether `last_5` appears in UI at all
- whether to rank by season only or by every window

## Recommendation

Use this default:

- show `season` and `last_10` in UI
- use `home/away` as a modifier
- use `season + last_10 + venue split` in models
- do not make `last_5` a primary signal

This gives strong interpretability, low implementation risk, and good predictive usefulness.
