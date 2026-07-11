#!/usr/bin/env python3
"""Snapshot and settle model-only steals/blocks tracking rows."""
import argparse, os, sqlite3
from datetime import datetime, timezone

from backend.app.db import connect
from backend.app.player_prop_model import MODEL_VERSION, predict_player_prop


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("snapshot", "settle"))
    args = parser.parse_args()
    tracking = sqlite3.connect(os.environ["WNBA_STOCKS_TRACKING_DB"])
    with connect() as conn:
        if args.action == "snapshot":
            rows = conn.execute("""SELECT DISTINCT g.id game_id,g.game_date,p.id player_id,p.full_name
              FROM prop_lines pl JOIN games g ON g.id=pl.game_id JOIN players p ON p.id=pl.player_id
              WHERE g.status='scheduled' AND EXISTS (SELECT 1 FROM player_game_stats s WHERE s.player_id=p.id)
              ORDER BY g.start_time,p.full_name""").fetchall()
            now = datetime.now(timezone.utc).isoformat()
            written = 0
            for row in rows:
                steals, _, _ = predict_player_prop(conn, row["player_id"], "steals", row["game_id"], allow_training=True)
                blocks, _, _ = predict_player_prop(conn, row["player_id"], "blocks", row["game_id"], allow_training=True)
                stocks = steals + blocks
                # Conservative Poisson approximation for model-only threshold tracking.
                import math
                p = lambda mean, n: 1 - sum(math.exp(-mean) * mean**k / math.factorial(k) for k in range(n))
                tracking.execute("""INSERT OR IGNORE INTO projection_snapshots
                  (game_id,player_id,player_name,game_date,captured_at,model_version,projected_steals,projected_blocks,projected_stocks,steal_prob_1_plus,steal_prob_2_plus,block_prob_1_plus,block_prob_2_plus,stocks_prob_2_plus,data_quality)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (row["game_id"],row["player_id"],row["full_name"],row["game_date"],now,MODEL_VERSION,steals,blocks,stocks,p(steals,1),p(steals,2),p(blocks,1),p(blocks,2),p(stocks,2),"model_only"))
                written += 1
            tracking.commit(); print({"snapshots": written})
        else:
            rows = tracking.execute("""SELECT ps.id,s.steals,s.blocks FROM projection_snapshots ps
              JOIN player_game_stats s ON s.game_id=ps.game_id AND s.player_id=ps.player_id
              LEFT JOIN settlements st ON st.snapshot_id=ps.id WHERE st.snapshot_id IS NULL""").fetchall()
            now = datetime.now(timezone.utc).isoformat()
            tracking.executemany("INSERT INTO settlements(snapshot_id,actual_steals,actual_blocks,settled_at) VALUES (?,?,?,?)", [(r[0],r[1],r[2],now) for r in rows])
            tracking.commit(); print({"settled": len(rows)})

if __name__ == "__main__": main()
