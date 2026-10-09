# Team scouting snapshots

## Observed RotoWire reports

`backend.app.rotowire_scouting` fetches the 15 WNBA team pages and stores the
nine scouting rows exactly as displayed, including the rank and original text
of each cell. Every successful HTTP fetch creates a new row in
`rotowire_scouting_snapshots` with its URL, UTC fetch time, page hash, and pull
ID. A repeated pull stores a new observation even when the values are unchanged.
The defensive FTA/FGA cell is preserved as displayed (for example, `0.3`),
without changing its scale.

The host cron runs at 1:00 a.m. and 11:00 a.m. `America/New_York` daily,
beginning September 24, 2026. The later pull captures completed-game reports
that RotoWire publishes after the overnight run. It invokes
`scripts.pull_rotowire_scouting`; its configured
`WNBA_SCOUTING_DB_PATH` must point to the active runtime SQLite volume. To run
an extra pull before the first scheduled date, use `--run-now`. The API also
exposes `POST /api/scouting/rotowire/pull` and
`GET /api/scouting/rotowire/snapshots` when a backend containing this code is
deployed.

### Game coverage investigation (September 24, 2026)

Commissioner's Cup qualifying games count as WNBA regular-season games, so they
are included in the regular-season scouting totals. The Cup championship is a
separate game and does not count toward the regular season. The current
RotoWire scouting values align with our reconstructed regular-season totals
when that championship is excluded; this supports excluding it from a
RotoWire-comparable backfill. See the [WNBA Cup rules](https://www.wnba.com/commissioners-cup/2026/about-the-cup)
and a [RotoWire team scouting report](https://www.rotowire.com/wnba/team/new-york-liberty-nyl).

Whether RotoWire adds playoff games to the scouting report is **unverified**.
The team page does not label the report's season phase, and we do not yet have
provider snapshots from both sides of a playoff game. During the postseason,
compare the same team's last pre-playoff report with reports fetched after its
first playoff game, allowing for RotoWire's update delay. Check the displayed
fields against regular-season-only and regular-season-plus-playoff calculations
before assigning a provider coverage rule. Our reconstructed snapshots already
extend cumulative totals through the playoffs; that is an internal choice and
does not establish RotoWire's behavior.

## Reconstructed historical reports

`backend.app.scouting_snapshots` builds one cumulative scouting snapshot for each
team after each completed regular-season or postseason game. Each snapshot is
keyed by game and team and stores season, phase, game counts, offense rates,
opponent rates, and pace. The postseason extends the same season totals while
keeping separate regular-season and postseason game counts.

These rows have a different source from the observed RotoWire rows. The source
is `team_game_results` plus both `team_game_boxscores` rows. ESPN
summary cache files identify the season phase. Preseason and Commissioner's Cup
championship games are excluded. A missing phase or box score aborts the build
before existing snapshots are replaced.

Run from the repository root against the intended SQLite database:

```bash
python3 -m scripts.backfill_scouting_snapshots --db-path /path/to/wnba.sqlite --cache-dir data/cache
```

Add `--season 2025` to replace only one season. Repeating the command produces
the same rows. The script does not download missing summaries; the cache must
contain the source phase metadata for every completed game being rebuilt.

Each rate uses summed counts through the snapshot game:

| Field | Calculation |
| --- | --- |
| Efficiency | Points per 100 possessions |
| Effective FG% | `(FGM + 0.5 × 3PM) / FGA` |
| Turnover % | Player turnovers / (FGA + 0.44 × FTA + player turnovers) |
| Offensive rebound % | Offensive rebounds / (offensive rebounds + opponent defensive rebounds) |
| FTA/FGA | Free throw attempts / field goal attempts |
| 3P%, 2P%, FT% | Makes / attempts of that shot type |
| 3PA/FGA | Three-point attempts / field goal attempts |
| Pace | Mean estimated possessions for both teams, normalized to 48 minutes |

The defense JSON applies the same calculations to opponent totals. Possessions
use `FGA - OREB + player turnovers + 0.44 × FTA`. Pace does not adjust for
overtime. Values are percentages, so
`fta_per_fga: 25.0` means 0.25 attempts per field goal attempt.

`latest_snapshot_before` selects only a snapshot with a start time strictly
earlier than the target game. This is the intended lookup for training and
pregame predictions.

## Pregame model features

`backend.app.scouting_features` reads reconstructed snapshots from the current
season with `game_date` strictly before the target game's date. The game
models use the same extractor for historical training and live prediction.
It supplies each team's offensive and defensive efficiency, pace, effective
field goal rate, turnover rate, and three-point attempt rate; five-game changes
in efficiency, pace, and effective field goal rate; and five matchup differences.
An availability flag and game count accompany each team. Missing history
produces zeros and a zero availability flag. These values use the reconstructed
defensive efficiency scale, not RotoWire's displayed defensive efficiency.

Six selected scouting features are appended to `GAME_DIRECT_FEATURE_NAMES`:
home and away effective-field-goal and turnover matchup differences, plus each
team's five-game offensive effective-field-goal change. Changing this set
invalidates the curated game training DB and model cache. The live database now
has reconstructed snapshots for 2024 (262 games), 2025 (311 games), and 2026
(325 games through September 23). Its 2024 cache was completed from the
repository's archived ESPN summaries before backfilling. Walk-forward
evaluation is required before relying on the new fields for model decisions.
The live model leaves them at zero by default. Set
`WNBA_GAME_SCOUTING_FEATURES=1` for an explicit training/evaluation run with
the values enabled; the training signature includes this setting.
The observed RotoWire rows stay separate: they began late in 2026 and are not
used as historical training inputs. Player prop models do not yet consume
scouting features.

Walk-forward comparisons favored the game model without scouting. With 2026
training only, ATS mean absolute error was 10.493 points without scouting,
10.666 with the initial 37 inputs, and 10.607 with the reduced six. With the
2024–2026 backfill, the reduced set had overall mean absolute error of 10.901
points versus 10.852 without it (2,688 evaluated predictions). ATS error was
10.318 versus 10.266; totals error was 11.950 versus 11.900. The reduced set
is therefore available for further experiments but disabled for live model
decisions.

The historical calculations have not been proven numerically identical to
RotoWire's unpublished formulas. Keep them marked as reconstructed; do not
present them as archived RotoWire observations. A current live comparison shows
shooting fields, offensive efficiency, and turnover rates align to the
displayed tenth when player turnovers and estimated possessions are used.
RotoWire's displayed defensive efficiency uses a different scale from standard
points allowed per 100 possessions. In the September 24, 2026 observation,
standard defensive rating differed by 21.5 points on average across 15 teams.
Scaling opponent points per estimated possession to 80 possessions reduced the
mean absolute difference to 1.0 point, but the largest team difference remained
2.1 points. This is evidence for an approximately 80-possession scale, not a
verified formula. Leave the reconstructed defense efficiency as the standard
per-100 value and use the exact fetched RotoWire value for observed snapshots.
