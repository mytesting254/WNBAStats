# Player Role Curation

This is the minimal curated-data layer for player opportunity roles.

Use it when raw box score history is not enough to explain why a player's shot or rebound opportunity moved.

## Why This Exists

Raw player and team stats already cover:

- rolling pace
- team FGA and opponent FGA allowed
- turnovers committed and forced
- missed-shot environment
- player minutes, FGA, rebounds, assists

Those are enough for broad environment features. They are not enough for stable role interpretation.

Examples that usually need curation:

- a player is the nominal point guard but not the primary initiator in current lineups
- a wing's rebound volume jumps because the team is intentionally crashing with that lineup
- a center's raw rebound rate looks stable, but the role changed after a frontcourt injury
- a player changes teams and the old rolling profile no longer matches the new usage hierarchy

## Scope

This layer curates player role labels and small bias multipliers. It does not curate pace or team-form metrics.

Rolling team environment should remain derived from raw historical team data.

## Runtime Table

Curated rows live in the app DB table:

- `player_role_bucket_overrides`

Each row can be scoped by:

- `player_id`
- optional `team_id`
- optional `effective_start_date`
- optional `effective_end_date`
- `priority`

Current curated fields:

- `ball_handler_bucket`
  - `primary`
  - `secondary`
  - `connector`
  - `finisher`
  - `interior`
  - `low_usage`
- `rebound_bucket`
  - `crash_big`
  - `wing_rebounder`
  - `guard_rebounder`
  - `leak_out`
  - `low_rebound`
- `shot_volume_bucket`
  - `alpha`
  - `secondary`
  - `tertiary`
  - `cleanup`
  - `low_usage`
- `offensive_rebound_bias`
- `defensive_rebound_bias`
- `field_goal_attempt_bias`
- `notes`

## Minimal Curation Policy

Do not try to curate the whole league manually.

Curate only players who meet at least one of these:

- recurring false positives in points or rebounds props
- visible role mismatch versus actual lineup usage
- recent transfer / injury return / rotation shock
- team-specific role that raw `position` and `rotation_role` do not capture

Start with:

1. primary and secondary handlers on each team
2. high-impact rebound specialists whose role is not captured by position alone
3. edge-case scorers whose shot role is clearly different from raw rotation labels

## Current Model Usage

The learned player-prop path now uses curated role overrides in two layers.

First, curated rows still reinforce archetype classification:

- `assist_guard`
- `rebound_big`
- `usage_scorer`

Second, the active learned prop feature set now includes explicit role features:

- ball-handler bucket flags
- rebound-role bucket flags
- shot-role bucket flags
- curated `field_goal_attempt_bias`
- curated `offensive_rebound_bias`
- curated `defensive_rebound_bias`

That promotion landed on Thursday, August 13, 2026.

On the same date, a separate team-opportunity feature block was tested alongside the role features:

- recent team misses
- recent opponent misses
- recent team offensive-rebound rate
- recent team defensive-rebound rate
- player recent scoring / rebound / assist shares

That block was removed from the active prop model after the completed walk-forward comparisons showed that:

- explicit role features helped
- the team-opportunity block hurt prop out-of-sample performance

So the current guidance is:

- keep curating role buckets and role bias multipliers
- do not treat team miss/rebound-share opportunity features as part of the active shipped prop stack

## Commands

List current overrides:

```bash
python scripts/curate_player_roles.py list
```

Add a row:

```bash
python scripts/curate_player_roles.py add \
  --player-id 123 \
  --team-id 7 \
  --ball-handler-bucket primary \
  --shot-volume-bucket secondary \
  --field-goal-attempt-bias 1.08 \
  --effective-start-date 2026-07-01 \
  --notes "Primary initiator after guard injury"
```

After changing curated roles, rebuild the curated player-prop DB so training artifacts invalidate cleanly:

```bash
python scripts/build_player_prop_training_db.py --force
```

## Intended Next Step

Once the override table has enough real rows, add a derived snapshot step that materializes the as-of-game role labels directly into curated player-prop training examples for easier audit and slice reporting.
