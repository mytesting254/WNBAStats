#!/usr/bin/env python3
"""
CLI tool for analyzing prediction accuracy and identifying best/worst projections.
Use this to evaluate which projections are closest to actual outcomes for parlay building.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.app.accuracy_analysis import build_accuracy_report, get_best_predictions, get_worst_predictions
from backend.app.db import connect


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analyze prediction accuracy and find reliable projections for parlays"
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    report_parser = subparsers.add_parser("report", help="Generate overall accuracy report")
    report_parser.add_argument(
        "--model",
        type=str,
        help="Filter by model version (e.g., adaptive-context-v1)",
    )

    best_parser = subparsers.add_parser("best", help="Show most accurate predictions")
    best_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Number of predictions to show (default: 20)",
    )
    best_parser.add_argument(
        "--min-edge",
        type=float,
        default=0.0,
        help="Minimum edge threshold (default: 0.0)",
    )
    best_parser.add_argument(
        "--confidence",
        type=str,
        choices=["high", "medium", "low"],
        help="Filter by confidence level",
    )
    best_parser.add_argument("--model", type=str, help="Filter by model version")

    worst_parser = subparsers.add_parser("worst", help="Show most inaccurate predictions")
    worst_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Number of predictions to show (default: 20)",
    )
    worst_parser.add_argument("--model", type=str, help="Filter by model version")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    with connect() as conn:
        if args.command == "report":
            report = build_accuracy_report(conn, args.model)
            print_report(report)
        elif args.command == "best":
            preds = get_best_predictions(
                conn,
                limit=args.limit,
                min_edge=args.min_edge,
                confidence_level=args.confidence,
                model_version=args.model,
            )
            print_predictions(preds, title="Most Accurate Predictions")
        elif args.command == "worst":
            preds = get_worst_predictions(conn, limit=args.limit, model_version=args.model)
            print_predictions(preds, title="Most Inaccurate Predictions")


def print_report(report) -> None:
    """Pretty-print overall accuracy report."""
    print("\n" + "=" * 80)
    print("PREDICTION ACCURACY REPORT")
    print("=" * 80)

    print(f"\nTotal Predictions Analyzed: {report.total_predictions}")
    print(f"\n{'Metric':<35} {'Value':>10}")
    print("-" * 50)
    print(f"{'Mean Absolute Error (MAE)':<35} {report.mae:>10.3f}")
    print(f"{'Root Mean Squared Error (RMSE)':<35} {report.rmse:>10.3f}")
    print(f"{'Mean Error / Bias':<35} {report.bias:>10.3f}")
    print(f"{'Directional Accuracy':<35} {report.directional_accuracy * 100:>9.1f}%")

    if report.confidence_calibration:
        print("\nCALIBRATION BY CONFIDENCE LEVEL")
        print("-" * 80)
        print(f"{'Level':<15} {'Count':<10} {'MAE':<10} {'Bias':<10} {'Dir. Acc.':<10}")
        for level, stats in sorted(report.confidence_calibration.items()):
            print(
                f"{level:<15} {stats['count']:<10} {stats['mae']:<10.2f} "
                f"{stats['bias']:<10.2f} {stats['directional_accuracy'] * 100:<9.1f}%"
            )

    if report.market_breakdown:
        print("\nBREAKDOWN BY MARKET")
        print("-" * 80)
        print(f"{'Market':<20} {'Count':<8} {'MAE':<10} {'RMSE':<10} {'Dir. Acc.':<10}")
        for market, stats in sorted(report.market_breakdown.items()):
            print(
                f"{market:<20} {stats['predictions']:<8} {stats['mae']:<10.2f} "
                f"{stats['rmse']:<10.2f} {stats['directional_accuracy'] * 100:<9.1f}%"
            )

    if report.error_by_minutes_played:
        print("\nERROR BY MINUTES PLAYED")
        print("-" * 80)
        for minutes_range, mae in sorted(report.error_by_minutes_played.items()):
            print(f"  {minutes_range:<10} minutes: MAE = {mae:.2f}")

    if report.performance_by_edge:
        print("\nPERFORMANCE BY EDGE THRESHOLD")
        print("-" * 80)
        print(f"{'Edge Threshold':<20} {'Predictions':<12} {'Correct':<10} {'Accuracy':<10} {'Avg Edge':<10}")
        for edge_threshold, stats in sorted(report.performance_by_edge.items()):
            print(
                f"{edge_threshold:<20} {stats['predictions']:<12} {stats['correct_picks']:<10} "
                f"{stats['accuracy'] * 100:<9.1f}% {stats['avg_edge']:<10.4f}"
            )

    print("\n" + "=" * 80)


def print_predictions(predictions, title: str) -> None:
    """Pretty-print list of predictions with accuracy metrics."""
    print("\n" + "=" * 120)
    print(title)
    print("=" * 120)

    if not predictions:
        print("No predictions found.")
        return

    print(
        f"\n{'Player':<20} {'Market':<15} {'Pred':<8} {'Actual':<8} {'Error':<8} "
        f"{'Conf':<8} {'Edge':<8} {'Rec':<5} {'Actual':<6} {'Hit':<3}"
    )
    print("-" * 120)

    for pred in predictions:
        hit = "Y" if pred.correct_side else "N"
        print(
            f"{pred.player_name:<20} {pred.market:<15} {pred.projection:<8.1f} "
            f"{pred.actual_result:<8.1f} {pred.error:+7.1f} {pred.confidence:<8} "
            f"{pred.edge:7.4f} {pred.rec_side:<5} {pred.actual_side:<6} {hit:<3}"
        )

    print("\n" + "=" * 120)


if __name__ == "__main__":
    main()
