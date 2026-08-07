from pathlib import Path
import argparse
import sys


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.app.db import connect, init_db
from backend.app.dfs_model import pretrain_dfs_half_models


def main() -> None:
    parser = argparse.ArgumentParser(description="Pretrain and persist DFS first-half market models.")
    args = parser.parse_args()
    del args

    init_db()
    with connect() as conn:
        result = pretrain_dfs_half_models(conn)
    print(result)


if __name__ == "__main__":
    main()
