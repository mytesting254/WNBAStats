from pathlib import Path
import argparse
import json
import sys


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.app.db import connect, init_db
from backend.app.dfs_model import DFS_HALF_MARKETS, evaluate_dfs_half_models


def main() -> None:
    parser = argparse.ArgumentParser(description="Walk-forward evaluation for DFS first-half models.")
    parser.add_argument("--markets", nargs="*", default=list(DFS_HALF_MARKETS), help="Markets to evaluate.")
    args = parser.parse_args()

    init_db()
    with connect() as conn:
        result = evaluate_dfs_half_models(conn)
    requested = {str(m).strip().lower() for m in args.markets}
    if requested and requested != set(DFS_HALF_MARKETS):
        result = {
            **result,
            "markets": {
                market: report
                for market, report in (result.get("markets") or {}).items()
                if market in requested
            },
            "gating": {
                **dict(result.get("gating") or {}),
                "requested_markets": sorted(requested),
            },
        }
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
