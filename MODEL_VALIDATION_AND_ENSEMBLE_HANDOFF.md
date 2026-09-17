# Cross-Sport Model Validation and Ensemble Handoff

## Scope

This document contains the reusable methodology developed during the NHL project. It intentionally excludes NHL-specific API ingestion, period settlement, team aliases, and NHL database schemas.

Each target sport must substitute its own:

- Event and schedule source.
- Team and player box-score source.
- Market and prop source.
- Settlement rules.
- Overtime, extra-period, push, and cancellation rules.
- Historical data timestamps.

The implementation principle is evaluation-first: no model component becomes live betting logic until it survives chronological validation against a realistic market baseline.

---

## 1. Data architecture

Maintain four conceptual layers.

### Raw source layer

Keep raw API or sportsbook responses unchanged where possible. Raw records are needed to audit:

- Capture time.
- Source/provider.
- Bookmaker.
- Event identifier.
- Market key.
- Line.
- Price.
- Player identity.
- Settlement status.

### Canonical market layer

Create a normalized table containing at least:

```text
event_id
market
line
home_price
away_price
over_price
under_price
sportsbook
captured_at
sportsbook_count
```

Do not replace raw history with only a consensus row. A consensus row is useful for modeling, but it cannot establish whether a price was executable or whether it was opening, current, or closing.

### Training-example layer

Create one row per event with only features available before the event:

- Team history.
- Player history.
- Rest and schedule context.
- Injuries/availability known at the cutoff.
- Market line and prices known at the cutoff.
- Target settlement fields.

### Settlement layer

Represent outcomes explicitly:

- Win.
- Loss.
- Push.
- Cancellation.
- Overtime/extra-period treatment.
- Void or unavailable market.

Never force a push into a binary loss.

---

## 2. Baselines

Every sport should report three baselines.

### Stats-only model

Uses historical team/player and situational features without current market prices.

### Market-only model

Uses the market line or implied probability as the forecast. This is the minimum benchmark for a betting model.

### Market-augmented model

Combines sports features with market information. It may be useful, but it is not proof of independent market alpha.

The report should show all three. A model that does not beat the market-only baseline should not be described as market-beating even if its internal accuracy is respectable.

---

## 3. Chronological validation

Random cross-validation is inappropriate for sports betting because games are time-dependent and the market evolves.

Use walk-forward validation:

1. Sort examples by event date and stable event identifier.
2. Train using earlier events.
3. Predict the next chronological block or season.
4. Append outcomes only after predictions are generated.
5. Continue forward.
6. Report every test block separately and in aggregate.

For a three-season dataset, a basic scheme is:

```text
train season 1 -> test season 2
train seasons 1-2 -> test season 3
```

A production-quality scheme should also use rolling or expanding weekly retraining.

All rolling features must be computed from prior observations only.

---

## 4. Target choices

For totals, maintain both:

### Absolute target

```text
actual_total
```

This supports MAE, RMSE, and distributional forecasts.

### Market-error target

```text
actual_total - market_line
```

The final projection is:

```text
market_line + predicted_market_error
```

The residual target focuses learning on where the market is wrong, but it must be compared against the market baseline and validated out of sample.

For binary markets, retain the continuous probability rather than only a hard class. Betting decisions should be made from calibrated probability and price.

---

## 5. Recency and regime features

Sports leagues change over time. Use recency weighting rather than assuming all historical games are equally informative.

One tested weighting function is:

```text
weight = 1 + 2 * exp(-age_days / 14)
```

This gives very recent events approximately 3x the influence of distant events while retaining older data.

Useful prior-only regime features include:

- Rolling market MAE.
- Rolling actual-minus-market residual.
- Rolling scoring environment.
- Rolling team shooting or conversion rate.
- Rest and compressed-schedule indicators.
- Recent player availability.
- Recent opponent-adjusted performance.

Do not calculate a rolling metric using the current event’s outcome.

---

## 6. Soft model switching

When multiple component models win in different regimes, use a smooth ensemble instead of hard thresholds.

Example:

```python
clipped = max(min(rolling_market_mae, 1.86), 1.81)
recency_weight = (clipped - 1.81) / (1.86 - 1.81)
market_weight = 1.0 - recency_weight
```

Then:

```text
final_projection =
    market_weight * market_model_projection
    + recency_weight * recency_error_projection
```

The endpoints must be selected from prior data only. Avoid switching rules that are tuned on the final test season.

The ensemble should record the active weight for every event so regime behavior can be audited.

---

## 7. Confidence and betting filters

Global MAE can hide the useful signal. Store the absolute model-market delta:

```text
delta = abs(model_projection - market_line)
```

Evaluate fixed buckets:

```text
0.0 <= delta < 0.5
0.5 <= delta < 1.0
delta >= 1.0
```

Fixed bins can become misleading when market efficiency changes. A dynamic alternative is a rolling percentile:

1. Keep prior deltas from the preceding 14 days.
2. Require a warmup minimum, such as 10 observations.
3. Compute the prior 85th percentile.
4. Bet only when the current delta exceeds that threshold.

This adjusts volume automatically when the market becomes tighter or noisier.

Every filter report must include:

- Number of eligible bets.
- Accuracy excluding pushes.
- Profit units.
- ROI after vig.
- Average delta.
- Season/block-level results.
- Minimum sample count.

A high accuracy with seven bets is not evidence of a stable edge.

---

## 8. Correct ROI calculation

For each one-unit bet:

- Win: return net payout according to American odds.
- Loss: lose one unit.
- Push: zero profit.
- Cancelled/void: exclude from denominator or handle according to the actual accounting policy.

Do not grade using an average price that was never available. Prefer:

1. Best executable price at prediction time.
2. Closing-line value.
3. Price and line movement after prediction.

Consensus historical rows are acceptable for preliminary diagnostics but not ideal for deployment conclusions.

---

## 9. Player-level ensemble design

A player layer should not simply sum the players who happen to have a prop line. That undercounts the full roster.

Use two stages:

### Opportunity model

Predict player opportunity:

- Minutes or usage.
- Starter probability.
- Role.
- Power-play or high-value usage.
- Recent workload.
- Rest and schedule.
- Availability.

### Rate model

Predict the event rate conditional on opportunity:

- Shots, attempts, possessions, or chances.
- Conversion/shooting rate.
- Defensive suppression.
- Opponent and venue effects.

For count outcomes, a Poisson or negative-binomial distribution is usually more appropriate than a single point estimate.

Aggregate simulated player distributions into team and event distributions.

The player model must use pre-event roster information. A post-event box-score roster is not a valid live feature.

---

## 10. Leakage-safe stacking

If a team model and player model feed a meta-learner, the meta-learner must see out-of-fold predictions.

Correct process:

1. Split chronologically.
2. Fit the component model on prior data.
3. Generate predictions for the next block.
4. Store those predictions.
5. Repeat until every training event has an out-of-fold prediction.
6. Fit the meta-learner only on those out-of-fold component predictions.
7. Evaluate the final stack on a later untouched block.

Possible meta-features:

```text
team_projection
player_projection
market_line
team_over_probability
player_over_probability
recent_market_mae
availability adjustment
goalie/defensive adjustment
```

A meta-learner trained on in-sample component predictions will produce optimistic and invalid results.

---

## 11. Feature quality standards

For every feature, document:

- Source table/API.
- Timestamp or cutoff.
- Missing-data behavior.
- Whether it is available before the event.
- Whether it is stable across seasons.
- Whether it duplicates market information.
- Whether it is a proxy rather than a direct measurement.

Do not invent unavailable advanced metrics. If xG, injuries, lineups, goalie identity, travel, or closing lines are missing, mark them unavailable and use an explicitly weaker proxy only after validation.

Proxy features must be reported separately from direct measurements.

---

## 12. Validation report standard

Every model report should contain:

### Coverage

- Number of events.
- Seasons and date ranges.
- Missing fields.
- Market-line availability.
- Price availability.
- Player coverage.

### Predictive metrics

- MAE.
- RMSE.
- Brier score for probabilities.
- Calibration.
- Accuracy excluding pushes.
- Market baseline metrics.

### Betting metrics

- Bet count.
- ROI.
- Profit units.
- Accuracy.
- Closing-line value where available.
- Results by season.
- Results by confidence bucket.

### Stability checks

- Threshold perturbation.
- Season/block consistency.
- Minimum bet count.
- Bootstrap confidence interval.
- Performance against a frozen threshold.
- Performance in a fully unseen time block.

Positive aggregate ROI with one profitable season and one losing season should be described as unstable, not as a proven edge.

---

## 13. Shadow-testing workflow

Before live use:

1. Run the model without placing bets.
2. Save every prediction with timestamp.
3. Save the exact line and price used.
4. Save the model weights and feature values.
5. Capture later line movement.
6. Grade the outcome after settlement.
7. Track ROI, calibration, and CLV.
8. Reassess only after a meaningful sample.

A minimum shadow period of one month is a starting point, not a guarantee. The first month should establish the current market-efficiency baseline before model thresholds are frozen.

---

## 14. Porting checklist

For each new sports project:

- [ ] Identify raw event and schedule source.
- [ ] Identify team and player outcome sources.
- [ ] Normalize market lines and prices.
- [ ] Preserve capture timestamps.
- [ ] Build canonical event examples.
- [ ] Define push, overtime, and cancellation rules.
- [ ] Build stats-only and market-only baselines.
- [ ] Add chronological residual features.
- [ ] Add recency weighting.
- [ ] Add smooth switching if regimes differ.
- [ ] Add rolling percentile confidence filters.
- [ ] Grade ROI after vig.
- [ ] Generate out-of-fold player predictions.
- [ ] Add a meta-learner only after OOF data exists.
- [ ] Run shadow testing.
- [ ] Keep unsuccessful components evaluation-only.


