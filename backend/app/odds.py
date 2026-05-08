from __future__ import annotations


def american_to_implied_probability(odds: int) -> float:
    if odds < 0:
        return abs(odds) / (abs(odds) + 100)
    return 100 / (odds + 100)


def american_profit_per_unit(odds: int) -> float:
    if odds < 0:
        return 100 / abs(odds)
    return odds / 100


def expected_value(probability: float, odds: int) -> float:
    profit = american_profit_per_unit(odds)
    return probability * profit - (1 - probability)

