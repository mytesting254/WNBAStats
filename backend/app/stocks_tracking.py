import math, os, sqlite3
from datetime import datetime, timezone

from .player_prop_model import MODEL_VERSION, predict_player_prop


def snapshot_stocks(conn, game_ids=None, runtime_cache=None):
    path = os.getenv("WNBA_STOCKS_TRACKING_DB", "").strip()
    if not path:
        return 0
    tracking = sqlite3.connect(path, timeout=30)
    tracking.execute("PRAGMA journal_mode=WAL")
    tracking.execute("PRAGMA busy_timeout=30000")
    filter_sql = "" if not game_ids else " AND g.id IN (" + ",".join("?" for _ in game_ids) + ")"
    rows = conn.execute("""SELECT DISTINCT g.id,g.game_date,p.id,p.full_name FROM games g
      JOIN players p ON p.team_id IN (g.home_team_id,g.away_team_id)
      WHERE g.status='scheduled' AND EXISTS (SELECT 1 FROM player_game_stats s WHERE s.player_id=p.id)
      AND COALESCE((SELECT lower(i.status) FROM injuries i WHERE i.player_id=p.id ORDER BY i.captured_at DESC,i.id DESC LIMIT 1),'available') NOT IN ('out','inactive')""" + filter_sql, tuple(game_ids or [])).fetchall()
    now = datetime.now(timezone.utc).isoformat(); written = 0
    for game_id, game_date, player_id, name in rows:
        steals, _, _ = predict_player_prop(conn, player_id, "steals", game_id, runtime_cache=runtime_cache, allow_training=False)
        blocks, _, _ = predict_player_prop(conn, player_id, "blocks", game_id, runtime_cache=runtime_cache, allow_training=False)
        prob = lambda mean, n: 1-sum(math.exp(-mean)*mean**k/math.factorial(k) for k in range(n))
        tracking.execute("""INSERT OR IGNORE INTO projection_snapshots (game_id,player_id,player_name,game_date,captured_at,model_version,projected_steals,projected_blocks,projected_stocks,steal_prob_1_plus,steal_prob_2_plus,block_prob_1_plus,block_prob_2_plus,stocks_prob_2_plus,data_quality) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (game_id,player_id,name,game_date,now,MODEL_VERSION,steals,blocks,steals+blocks,prob(steals,1),prob(steals,2),prob(blocks,1),prob(blocks,2),prob(steals+blocks,2),"model_only")); written += 1
    tracking.commit(); tracking.close(); return written
