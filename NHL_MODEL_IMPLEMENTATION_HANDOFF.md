# NHL Model Implementation Handoff

## Purpose

This document records the NHL work completed in `/root/NHLProps`, the design decisions, validation methods, known limitations, and the porting workflow for other sports projects.

The NFL project was used only as conceptual inspiration. No NFL files were modified.

The implementation covers:

1. Historical game and period-market ingestion.
2. Official NHL period-score settlement.
3. Period game-prop models.
4. Game-total and over/under models.
5. Player-derived total signals.
6. Market-regime switching.
7. ROI and confidence-filter diagnostics.
8. Chronological leakage-controlled validation.

The implementation is evaluation-first. A model is not promoted to live betting logic merely because it has a positive historical result.

---

## 1. Runtime and storage

The project uses Python, SQLite, and a local virtual environment:

```text
/root/NHLProps/.venv
```

Typical commands:

```bash
cd /root/NHLProps
./.venv/bin/python <script>
./.venv/bin/pytest
```

Primary runtime database:

```text
data/nhl.sqlite
```

Curated training databases:

```text
data/nhl-game-training.sqlite
data/nhl-player-prop-training.sqlite
data/nhl-toi-training.sqlite
data/nhl-team-training.sqlite
```

Supported path overrides:

```text
NHL_DB_PATH
NHL_DATA_DIR
NHL_GAME_TRAINING_DB_PATH
NHL_PLAYER_PROP_TRAINING_DB_PATH
NHL_TOI_TRAINING_DB_PATH
```

API credentials are intentionally not recorded in this document. Supply them through the environment or a local secret mechanism.

---

## 2. Historical odds ingestion

### Period markets

The historical odds layer was extended with:

```text
h2h_p1
h2h_p2
h2h_p3
h2h_3_way_p1
h2h_3_way_p2
h2h_3_way_p3
```

Relevant files:

```text
backend/app/odds_history.py
backend/app/odds_import.py
scripts/backfill_historical_game_markets.py
```

`GAME_MARKETS` includes featured game markets and the six period markets. `DEFAULT_MARKETS` includes game markets and supported player props.

### Retry behavior

Historical API requests retry transient HTTP 502, 503, and 504 failures and URL errors, with up to three attempts and short backoff. This is needed for large backfills.

### Backfill coverage

The period backfill covered 4,192 games and produced approximately 194,804 raw period market rows.

Canonical two-way period counts:

```text
h2h_p1: 4,192
h2h_p2: 4,192
h2h_p3: 4,192
```

Canonical three-way period counts:

```text
h2h_3_way_p1: 2,369
h2h_3_way_p2: 2,369
h2h_3_way_p3: 2,369
```

The three-way markets therefore have incomplete historical coverage. A future three-way evaluator must report coverage explicitly.

### Canonical table

`backend/app/db.py` now creates `period_market_lines` with:

```sql
game_id
period
market
home_moneyline
away_moneyline
draw_moneyline
captured_at
```

Rows are unique by `(game_id, market)`.

Inspection endpoint:

```text
GET /api/period-market-lines?game_id=<id>
```

Raw sportsbook rows remain separate from canonical rows. This is important because consensus data is not the same as an executable sportsbook price.

---

## 3. Official NHL period outcomes

Period settlement is sourced from the official NHL API, not inferred from odds:

```text
https://api-web.nhle.com/v1/score/{date}
https://api-web.nhle.com/v1/gamecenter/{nhl_game_id}/landing
```

The landing endpoint contains period scoring under `summary.scoring`.

Implementation:

```text
scripts/backfill_nhl_period_scores.py
```

Storage table:

```text
game_period_scores
```

Important fields:

```text
game_id
nhl_game_id
period
home_goals
away_goals
captured_at
```

Team aliases handled include LA/LAK, NJ/NJD, SJ/SJS, TB/TBL, UTAH/UTA.

Final coverage:

```text
games:              4,446
games with scores:  4,446
period rows:        13,338
missing games:      0
```

Period ties are common:

```text
P1: 1,523 / 4,446 = 34.3%
P2: 1,330 / 4,446 = 29.9%
P3: 1,176 / 4,446 = 26.5%
```

An early validation version incorrectly counted ties as away wins and produced falsely large ROI. The evaluator was corrected so ties are pushes and excluded from accuracy denominators.

---

## 4. Curated game training data

The primary table is:

```text
game_training_examples
```

Current coverage is 3,934 examples:

```text
2023-24: 1,315
2024-25: 1,311
2025-26: 1,308
```

Fields include:

- Home and away teams.
- Games played prior to the event.
- Prior win percentage.
- Prior goals for/against.
- Prior shots for/against.
- Prior blocked shots for/against.
- Power-play percentage.
- Faceoff percentage.
- Penalties and penalty minutes.
- Rest days.
- Spread, total, moneyline, and over/under prices.
- Implied probabilities.
- Final home score, away score, total goals, and margin.

The current target `total_goals` is the recorded final score total, including overtime/shootout scoring where the source records it. It is not capped at 60 minutes.

The existing base feature vector has 26 fields. The first 16 are team/context features; later fields contain market information. Therefore every report compares:

1. Stats-only model.
2. Market-augmented model.
3. Market baseline.

A market-augmented model is market-informed, not proof of independent market alpha.

---

## 5. Period models

Training module:

```text
backend/app/period_baseline_training.py
```

Training script:

```text
scripts/train_period_models.py
```

Supported periods:

```text
P1, P2, P3
```

Supported targets:

```text
home_win
home_margin
total_goals
```

Period features extend the existing game vector with:

```text
period_home_moneyline
period_away_moneyline
period_draw_moneyline
period_implied_home_win_prob
period_implied_away_win_prob
```

The baseline uses Ridge regression.

Initial holdout metrics were approximately:

```text
P1 home-win Brier: 0.2247
P1 accuracy:       0.6709
P1 margin MAE:     1.0156
P1 total MAE:      1.0654

P2 home-win Brier: 0.2329
P2 accuracy:       0.6277
P2 margin MAE:     1.1389
P2 total MAE:      1.0432

P3 home-win Brier: 0.2326
P3 accuracy:       0.6290
P3 margin MAE:     1.1635
P3 total MAE:      1.1158
```

Validation module and report:

```text
backend/app/period_validation.py
scripts/validate_period_models.py
data/NHL_PERIOD_MODEL_VALIDATION_REPORT.md
```

Validation is chronological:

1. Train on earlier seasons.
2. Test on the next season.
3. Calibrate with prior-season empirical bins.
4. Compare against canonical period lines.
5. Evaluate EV thresholds.
6. Freeze only a prior-positive threshold.
7. Otherwise place no bets.

After tie correction, P1/P2/P3 did not establish a positive frozen betting threshold. This is a valid no-bet result.

---

## 6. Game-total and over/under modeling

Implementation:

```text
backend/app/total_model.py
scripts/validate_total_model.py
data/NHL_TOTAL_MODEL_VALIDATION_REPORT.md
```

### Models

#### Stats-only

Uses the first 16 non-market team/context features.

#### Market-augmented

Uses the complete 26-field base vector, including game total and prices.

#### Hidden-feature model

Adds features reconstructed from available box scores:

```text
recent_residual_3_delta
recent_residual_5_delta
recent_shooting_pct_5_delta
home_back_to_back
away_back_to_back
home_three_in_four
away_three_in_four
home_backup_goalie_proxy
away_backup_goalie_proxy
```

#### Recency market-error model

Predicts:

```text
actual_total - sportsbook_total
```

and adds the predicted error to the sportsbook line.

Training weights are:

```text
weight = 1 + 2 * exp(-age_days / 14)
```

Recent data therefore receives more influence without discarding older seasons.

#### Switching ensemble

The switching model combines market-augmented and recency-error predictions. Rolling market MAE is calculated only from prior completed games.

Smooth weights:

```text
clipped_mae = clip(rolling_market_mae, 1.81, 1.86)
recency_weight = (clipped_mae - 1.81) / (1.86 - 1.81)
market_weight = 1 - recency_weight
```

This avoids abrupt 1.82/1.85 behavioral cliffs.

### Market distribution

Current game totals:

```text
5.0: 9
5.5: 1,014
6.0: 1,684
6.5: 1,421
7.0: 64
```

Approximately 74% of lines are 6.0 or 6.5.

### Walk-forward results

Mean season MAE values:

```text
stats_only:              1.8479
market_augmented:        1.8364
hidden_market_augmented: 1.8392
recency_market_error:    1.8392
switching_ensemble:      approximately 1.8370 after smooth switching
```

The earlier hard-switch implementation recorded approximately 1.8356. The smooth version is slightly worse in MAE but is less brittle.

Market baseline MAE differed by season:

```text
2024-25: 1.8581
2025-26: 1.8169
```

This regime shift explains why a fixed strategy did not transfer cleanly.

---

## 7. Betting and confidence diagnostics

For every prediction:

1. Over is selected if projection exceeds the total.
2. Under is selected if projection is below the total.
3. Exact-line results are treated as pushes/no directional result.
4. Cached over/under American prices are used.
5. One unit is risked per bet.
6. ROI is profit units divided by bets.

### Fixed delta buckets

The report evaluates:

```text
0.0 <= abs(projection - line) < 0.5
0.5 <= abs(projection - line) < 1.0
abs(projection - line) >= 1.0
```

The 0.5–1.0 bucket was unstable:

```text
2024-25 market-augmented: +5.76% over 199 bets
2025-26 market-augmented: -3.32% over 110 bets
```

Large deltas were too sparse to trust.

### Dynamic top-15% filter

The switching ensemble now stores prior absolute deltas. After at least 10 warmup values, it qualifies only games at or above the rolling 85th percentile of absolute deltas over the prior 14 days.

Observed cached-price results:

```text
2024-25: 192 bets, approximately +4.41% ROI
2025-26: 198 bets, approximately +4.75% ROI
```

This is more stable than the fixed 0.5–1.0 bucket but is not yet a deployment-grade conclusion because:

- Only three seasons exist.
- Prices may be consensus rather than executable.
- Opening/closing timing is incomplete.
- No future-season holdout has been observed.

The report includes sample count, accuracy, ROI, and profit units.

---

## 8. Player data and bottom-up totals

Player-prop training data contains approximately 280,354 rows across 4,192 games.

Markets:

```text
player_assists
player_blocked_shots
player_goals
player_points
player_shots_on_goal
player_total_saves
```

Every curated player-prop row has TOI results. There are also TOI training databases with skater and goalie rolling features.

Implementation:

```text
backend/app/player_total_ensemble.py
scripts/validate_player_total_blend.py
```

### Prop-only player signal

The first player signal summed predicted player-goal prop values. It was rejected because:

- Props cover only a subset of players.
- Summing available props undercounts a full roster.
- Coverage correction was too crude.
- The signal worsened total MAE.

### All-skater signal

The second signal used prior team-game history:

- Candidate roster players came from prior games.
- Current-game box scores were appended after prediction.
- Recent TOI estimated opportunity.
- Goals per minute estimated scoring.
- Later version used shots per minute times shooting percentage.
- Recent history filtering prevented stale roster entries from remaining active.

Results:

```text
team model MAE:       1.836
all-player MAE:       2.083
50/50 blend MAE:      1.903
```

The player layer is therefore evaluation-only and is not wired into live predictions.

Correct next design:

1. Predict every likely skater's TOI.
2. Predict shots.
3. Predict shooting percentage with shrinkage.
4. Add opponent and goalie suppression.
5. Simulate goals with Poisson or negative binomial distributions.
6. Generate out-of-fold player predictions.
7. Use the player result as a calibrated veto or meta-learner feature.

A player veto must not be activated merely because an uncalibrated player projection disagrees with the team model.

---

## 9. Hidden features

Module:

```text
backend/app/hidden_total_features.py
```

### Residual form

A team’s recent residual proxy is:

```text
team goals - game total / 2
```

Rolling three-game and five-game deltas are compared across the matchup.

### Shooting percentage

```text
goals / shots
```

Recent team shooting is used as a weak mean-reversion signal.

### Fatigue

Back-to-back is flagged when the previous game date is within one day.

Three-in-four is flagged when at least two prior games fall in the preceding three-day window.

### Backup goalie proxy

A team is flagged when:

1. It is on a back-to-back.
2. The prior-game goalie proxy recorded more than 30 saves.

The goalie proxy is a heuristic based on available goalie box-score saves. It is not a confirmed starter feed.

### Not available

The database does not currently support reliable:

```text
GSAE
shot-location danger
cross-slot versus point-shot splits
line chemistry
confirmed line matching
opening/closing RLM
historical injury snapshots
empty-net labels
travel border-crossing data
```

These must not be fabricated from unrelated box-score fields.

---

## 10. Leakage controls

Chronological leakage controls are mandatory:

- Train seasons precede test seasons.
- Period outcomes are targets, never pregame inputs.
- Hidden features are constructed before the current game is appended.
- Rolling market MAE excludes the current game.
- Rolling delta percentiles exclude the current game.
- Current-game player box scores are appended after player prediction.
- Ties and pushes are not forced into wins/losses.
- Player roster information must be available before puck drop to qualify as a real feature.
- Future meta-learners must train only on out-of-fold component predictions.

A meta-learner trained on in-sample team/player predictions will overstate performance.

---

## 11. Data limitations

### Only three seasons

The game training set is 3,934 examples over three seasons. This is enough for a baseline, not enough for strong regime claims.

### One canonical game-market row

There are 4,192 game-market rows across 4,192 games, with 2,555 distinct capture timestamps. This is not a complete opening-to-closing time series.

Consequences:

- RLM cannot be measured reliably.
- Closing-line value cannot be measured.
- Consensus may not be directly executable.
- Market-timing effects remain unresolved.

### Empty injuries table

The injuries table currently contains no usable historical rows. Injury-aware claims are therefore unsupported.

### Market dependence

The market-augmented model uses the market as a feature and is compared against the market. It should be interpreted as a market-adjustment model, not independent proof of alpha.

### ROI uncertainty

Positive ROI must survive:

- Multiple chronological holdouts.
- Minimum bet counts.
- Vig and push handling.
- Price availability.
- Threshold perturbations.
- Out-of-sample testing.
- Ideally, closing-line-value analysis.

---

## 12. Reproduction commands

Compile:

```bash
./.venv/bin/python -m compileall -q backend/app scripts
```

Train periods:

```bash
./.venv/bin/python scripts/train_period_models.py --db-path data/nhl.sqlite
```

Validate periods:

```bash
./.venv/bin/python scripts/validate_period_models.py --db-path data/nhl.sqlite
```

Validate totals, switching, ROI, and dynamic filtering:

```bash
./.venv/bin/python scripts/validate_total_model.py \
  --db-path data/nhl.sqlite \
  --output data/NHL_TOTAL_MODEL_VALIDATION_REPORT.md
```

Validate prop-only player blend:

```bash
./.venv/bin/python scripts/validate_player_total_blend.py \
  --db-path data/nhl.sqlite \
  --output data/NHL_PLAYER_TOTAL_BLEND_REPORT.md
```

Validate all-player blend:

```bash
./.venv/bin/python scripts/validate_player_total_blend.py \
  --db-path data/nhl.sqlite \
  --all-players \
  --output data/NHL_ALL_PLAYER_TOTAL_BLEND_REPORT.md
```

Focused tests:

```bash
./.venv/bin/pytest -q \
  tests/test_runtime_config.py \
  tests/test_rotowire_import.py
```

The focused tests pass. A slow existing health test should be investigated separately.

---

## 13. Files added or changed

Core database/API:

```text
backend/app/db.py
backend/app/main.py
backend/app/odds_history.py
backend/app/odds_import.py
```

Ingestion:

```text
scripts/backfill_historical_game_markets.py
scripts/backfill_nhl_period_scores.py
```

Period models:

```text
backend/app/period_baseline_training.py
backend/app/period_validation.py
scripts/train_period_models.py
scripts/validate_period_models.py
```

Total models:

```text
backend/app/total_model.py
backend/app/hidden_total_features.py
scripts/validate_total_model.py
```

Player totals:

```text
backend/app/player_total_ensemble.py
scripts/validate_player_total_blend.py
```

Generated reports:

```text
data/NHL_PERIOD_MODEL_VALIDATION_REPORT.md
data/NHL_TOTAL_MODEL_VALIDATION_REPORT.md
data/NHL_PLAYER_TOTAL_BLEND_REPORT.md
data/NHL_ALL_PLAYER_TOTAL_BLEND_REPORT.md
data/models/periods/
```

Generated artifacts should normally be rebuilt for another project rather than copied blindly.

---

## 14. Porting to another sports project

Port the pattern, not NHL-specific names.

### Phase A: source audit

Identify:

- Event/game table.
- Team box scores.
- Player box scores.
- Market lines.
- Prop lines.
- Capture timestamps.
- Settlement outcomes.
- Push/cancellation semantics.

Do not train until every feature has a pre-event cutoff.

### Phase B: canonical market layer

Maintain raw rows and a canonical table containing:

```text
event_id
market_type
line
prices
captured_at
sportsbook_count
```

Never discard raw market history.

### Phase C: outcome layer

Represent win, loss, push, cancellation, and overtime rules explicitly.

### Phase D: baseline

Implement and report:

1. Stats-only model.
2. Market-only baseline.
3. Market-augmented model.
4. Walk-forward season validation.

### Phase E: regime features

Add prior-only residuals, rolling market MAE, recency weights, and smooth switching.

### Phase F: confidence filters

Evaluate fixed delta, rolling percentile delta, probability edge, ROI, and minimum sample sizes. Freeze thresholds using prior data only.

### Phase G: player ensemble

Build opportunity projections, rate projections, distributions, out-of-fold forecasts, and then a veto/meta-learner.

### Phase H: shadow testing

Run at least one month without betting:

- Store prediction timestamp.
- Store exact market snapshot.
- Store available price and line.
- Track closing-line movement.
- Grade results and CLV.

---

## 15. Recommended next work

1. Backfill full market snapshot history for opening/closing lines and CLV.
2. Add confirmed historical goalie starters.
3. Improve all-player TOI and shots projections.
4. Generate true out-of-fold player simulations.
5. Add three-way period models with draw-aware EV.
6. Add confidence intervals for ROI.
7. Test dynamic filters on a future unseen season.
8. Keep every new component in shadow mode until it beats a realistic executable market baseline.

The current switching model is the strongest implemented total component, but the evidence supports continued validation rather than automatic deployment.

