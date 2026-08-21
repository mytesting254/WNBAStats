# Settled Prop Miss Fix List - 2026-08-21

## Scope

This fix list is derived from the largest settled prop misses in the live runtime database as of 2026-08-21.

The goal is not to redesign the entire system. The goal is to fix the repeated failure modes that show up in the worst misses.

## What The Misses Showed

The biggest misses were concentrated in a small number of patterns:

1. nonpositive or near-zero projections for active players with normal minutes
2. combo markets (`points_rebounds`, `points_assists`, `points_rebounds_assists`) carrying the heaviest large-error rate
3. high-confidence labels on projections that were still too fragile
4. early-exit / very low-minute games creating catastrophic misses that should be separated from normal projection quality
5. star-usage ceiling games being suppressed too hard by mean reversion

Representative examples from the live settled rows:

- Paige Bueckers on 2026-07-19 had multiple `component-pregame-v2` combo and points projections near `0`, while actual results landed in the `25-34` range
- Aaliyah Edwards had `adaptive-context-v1` settled rows with `0.0` projection and actuals in the `24-28` range
- Jonquel Jones and Kelsey Plum had large misses tied to `6` minutes and `0` minutes respectively, which are real misses but should be treated differently from normal full-minute model failures

## Priority 1: Add Projection Safety Floors

### Problem

Some of the worst misses are not normal forecasting error. They are pathological outputs: negative or near-zero projections for counting stats.

### What to change

Add a projection floor layer in the live prop path after raw learned/component estimation and before recommendation logic.

Primary change areas:

- [backend/app/player_prop_model.py](/root/WNBAStats/backend/app/player_prop_model.py:539)
- [backend/app/player_prop_model.py](/root/WNBAStats/backend/app/player_prop_model.py:844)
- [backend/app/player_prop_model.py](/root/WNBAStats/backend/app/player_prop_model.py:870)

### Desired behavior

- never publish a negative projection for any counting stat market
- reject near-zero projections for players with meaningful projected minutes
- if projection is inconsistent with role and minute expectation, fall back to a safe anchor

### Concrete rule

For `points`, `rebounds`, `assists`, `threes`, and combo markets:

- hard floor at `0.0`
- if projected minutes are above a role threshold and projection is below a market minimum, replace with a floor anchor

Example anchor sources:

- recent rate x projected minutes
- recent median outcome over last `5`
- conservative component projection floor

### Why it matters

This single change removes a class of avoidable catastrophic misses.

## Priority 2: Add Role-Aware Floor Fallbacks

### Problem

A hard zero floor is not enough. Some starters and stable rotation players are still getting implausibly low projections.

### What to change

Build a role-and-minutes consistency gate inside the prop projection path.

Primary change areas:

- [backend/app/player_prop_model.py](/root/WNBAStats/backend/app/player_prop_model.py:1055)
- [backend/app/player_prop_model.py](/root/WNBAStats/backend/app/player_prop_model.py:3265)
- [backend/app/player_prop_model.py](/root/WNBAStats/backend/app/player_prop_model.py:4909)

### Desired behavior

If all of the following are true:

- player projected minutes are healthy
- player has stable recent minutes
- player role is `starter` or stable `rotation`
- learned or component output is implausibly low

then the projection should be lifted to a conservative floor.

### Suggested floor logic

- `points`: no lower than `0.55 * (recent_rate * projected_minutes)` for stable starters
- `points_rebounds` / `points_assists`: no lower than `0.65 *` sum-of-component floor
- `PRA`: no lower than `0.70 *` sum-of-component floor

The exact constants should be tuned, but the mechanism is more important than the initial values.

## Priority 3: Treat Combo Markets As A Separate Risk Class

### Problem

Combo markets are consistently the worst large-miss markets.

Live settled summary showed high large-error rates in:

- `points_rebounds_assists`
- `points_rebounds`
- `points_assists`

### What to change

Continue pushing combo markets toward stricter policy than singles.

Primary change areas:

- [backend/app/player_prop_model.py](/root/WNBAStats/backend/app/player_prop_model.py:229)
- [backend/app/player_prop_model.py](/root/WNBAStats/backend/app/player_prop_model.py:664)
- [backend/app/projections.py](/root/WNBAStats/backend/app/projections.py:57)
- [backend/app/projections.py](/root/WNBAStats/backend/app/projections.py:1033)

### Desired behavior

- combo markets should rarely qualify as `high`
- combo markets should require stronger edge and stronger normalized margin
- combo markets should fall back to component logic more often than singles

### Next tightening pass

- cap `PRA`, `points_rebounds`, `points_assists` at `medium` unless recent settled evidence proves otherwise
- require stronger learned-vs-component advantage before any combo overlay is used live

## Priority 4: Separate Early-Exit Games From Normal Projection Evaluation

### Problem

Games with `0-10` minutes generate huge misses, but they should not be mixed with normal projection quality when calibrating the core model.

### What to change

Add an explicit evaluation cohort for early-exit / very low-minute settled props.

Primary change areas:

- [backend/app/main.py](/root/WNBAStats/backend/app/main.py:3302)
- [backend/app/projections.py](/root/WNBAStats/backend/app/projections.py:1263)

### Desired behavior

Split settled evaluation into:

- normal-minute outcomes
- low-minute outcomes (`player_minutes <= 10`)

### Why it matters

This does not mean ignoring those misses. It means:

- do not let them distort confidence calibration for normal games
- separately decide whether a new “minutes collapse risk” adjustment is needed

### Follow-up improvement

Build a light pregame fragility flag using:

- recent injury status
- questionable / doubtful / GTD context
- volatile minutes role
- return-from-absence patterns

Relevant logic already exists around:

- [backend/app/player_prop_model.py](/root/WNBAStats/backend/app/player_prop_model.py:6594)
- [backend/app/player_prop_model.py](/root/WNBAStats/backend/app/player_prop_model.py:7124)

## Priority 5: Reduce Ceiling Suppression For Star Usage Players

### Problem

Some high-usage scorers and primary creators are still being pulled too aggressively toward average outcomes.

This showed up in large underestimates for players like:

- Caitlin Clark
- Kahleah Copper
- Brittney Sykes
- NaLyssa Smith

### What to change

Adjust shrinkage and stabilization behavior for alpha-usage players.

Primary change areas:

- [backend/app/player_prop_model.py](/root/WNBAStats/backend/app/player_prop_model.py:870)
- [backend/app/player_prop_model.py](/root/WNBAStats/backend/app/player_prop_model.py:1055)
- [backend/app/player_prop_model.py](/root/WNBAStats/backend/app/player_prop_model.py:1519)

### Desired behavior

For players with strong usage signals:

- reduce downward stabilization when projected minutes are intact
- allow a larger ceiling band when role and opportunity support it
- rely more on role/archetype-aware anchors and less on plain mean reversion

Signals already available in the feature set:

- role/archetype fields
- injury / opportunity context
- recent position minute share
- same-position unavailable minutes

### Specific approach

Create a “ceiling-preservation” path for:

- `arch_usage_scorer`
- `role_primary_handler`
- `role_alpha_shot`

and apply weaker low-side clipping when:

- projected minutes are stable
- injury context is not negative
- opportunity context is positive

## Priority 6: Audit All Nonpositive Settled Projections

### Problem

Nonpositive settled projections are rare but high signal. They are overrepresented in the worst miss set.

### What to change

Add a dedicated audit report for any settled row where:

- `projection <= 0`
- player logged meaningful minutes
- actual result was materially above zero

Primary change areas:

- [backend/app/main.py](/root/WNBAStats/backend/app/main.py:3302)
- [backend/app/main.py](/root/WNBAStats/backend/app/main.py:6843)

### Desired behavior

Expose a report with:

- player
- market
- model_version
- projected minutes
- projection
- actual result
- role
- injury context
- opportunity context

This should become a standard regression watchlist.

## Priority 7: Tighten High-Confidence Gates For Scoring-Heavy Markets

### Problem

The miss review showed that some `high` confidence labels are still too permissive, especially for scoring and combo markets.

### What to change

Tighten `high` confidence eligibility for:

- `points`
- `points_rebounds`
- `points_assists`
- `points_rebounds_assists`

Primary change area:

- [backend/app/projections.py](/root/WNBAStats/backend/app/projections.py:1033)

### Desired behavior

Require:

- stronger sample quality
- stronger normalized margin
- stronger edge
- no fragile-minute signals

### Extra guard

If projected minutes are volatile or injury context is uncertain, cap confidence at `medium`.

## Priority 8: Build A “Catastrophic Miss” Review Dataset

### Problem

The highest-value debugging set is not “all misses.” It is the small tail of worst misses.

### What to change

Create a reproducible extract of rows where:

- `abs(projection - actual_result) >= 12`

and enrich it with:

- role
- projected minutes
- actual minutes
- injury / availability status
- blowout flag
- market family
- model version

Primary change areas:

- [backend/app/main.py](/root/WNBAStats/backend/app/main.py:3302)
- [backend/app/projections.py](/root/WNBAStats/backend/app/projections.py:1263)

### Why it matters

That dataset should drive future model changes. It is more useful than aggregate MAE alone.

## Implementation Order

### First pass

1. Add hard projection floor and nonnegative guard.
2. Add role/minutes consistency fallback.
3. Tighten combo-market and high-confidence rules.
4. Split early-exit evaluation from normal-minute evaluation.

### Second pass

1. Add ceiling-preservation logic for star-usage players.
2. Add nonpositive-projection audit reporting.
3. Add catastrophic-miss review extract.

## Expected Impact

These changes should improve the system by:

- removing obviously broken outputs
- reducing catastrophic combo misses
- making `high` confidence more trustworthy
- separating bad luck / early exits from true model weakness
- preserving more upside for legitimate star-usage ceiling games

## Bottom Line

The settled misses say the next projection gains should come from:

- projection safety
- combo-market conservatism
- confidence tightening
- minutes-risk separation
- ceiling preservation for the right player types

This is concrete model-improvement work. It is not a dead end, and it does not require a blind full retrain first.
