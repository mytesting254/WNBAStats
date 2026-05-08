from __future__ import annotations

from datetime import datetime, timezone

from .db import connect, init_db
from .projections import rebuild_predictions


def seed_sample_data() -> None:
    init_db()
    captured_at = datetime.now(timezone.utc).isoformat()
    with connect() as conn:
        conn.executescript(
            """
            DELETE FROM prop_predictions;
            DELETE FROM sportsbook_prop_lines;
            DELETE FROM model_runs;
            DELETE FROM settled_props;
            DELETE FROM prop_lines;
            DELETE FROM manual_adjustments;
            DELETE FROM injuries;
            DELETE FROM player_game_stats;
            DELETE FROM team_game_results;
            DELETE FROM games;
            DELETE FROM players;
            DELETE FROM teams;
            """
        )
        conn.executemany(
            "INSERT INTO teams (id, abbreviation, name, logo_url) VALUES (?, ?, ?, ?)",
            [
                (1, "LV", "Las Vegas Aces", "/team-logos/lv.png"),
                (2, "IND", "Indiana Fever", "/team-logos/ind.png"),
                (3, "NY", "New York Liberty", "/team-logos/ny.png"),
                (4, "MIN", "Minnesota Lynx", "/team-logos/min.png"),
                (5, "CON", "Connecticut Sun", "/team-logos/conn.png"),
                (6, "DAL", "Dallas Wings", "/team-logos/dal.png"),
                (7, "LA", "Los Angeles Sparks", "/team-logos/la.png"),
                (8, "ATL", "Atlanta Dream", "/team-logos/atl.png"),
                (9, "CHI", "Chicago Sky", "/team-logos/chi.png"),
                (10, "PHX", "Phoenix Mercury", "/team-logos/phx.png"),
                (11, "WSH", "Washington Mystics", "/team-logos/wsh.png"),
                (12, "TOR", "Toronto Tempo", "/team-logos/tor.png"),
                (13, "GS", "Golden State Valkyries", "/team-logos/gs.png"),
                (14, "SEA", "Seattle Storm", "/team-logos/sea.png"),
            ],
        )
        conn.executemany(
            "INSERT INTO players (id, full_name, team_id, position, rotation_role) VALUES (?, ?, ?, ?, ?)",
            [
                (101, "A'ja Wilson", 1, "F", "star"),
                (102, "Caitlin Clark", 2, "G", "star"),
                (103, "Breanna Stewart", 3, "F", "star"),
                (104, "Napheesa Collier", 4, "F", "star"),
                (105, "Sabrina Ionescu", 3, "G", "starter"),
                (106, "Marina Mabrey", 5, "G", "star"),
                (107, "Tina Charles", 5, "F", "starter"),
                (108, "Sonia Citron", 9, "G", "star"),
                (109, "Aaliyah Edwards", 9, "F", "starter"),
                (110, "Diana Taurasi", 10, "G", "star"),
                (111, "Brittney Griner", 10, "C", "starter"),
                (112, "Skylar Diggins", 11, "G", "star"),
                (113, "Nneka Ogwumike", 11, "F", "starter"),
                (114, "Betnijah Laney", 12, "G", "star"),
                (115, "DeWanna Bonner", 12, "F", "starter"),
            ],
        )
        games = []
        for i in range(10):
            games.append((300 + i, f"2025-09-{10 + i:02d}", f"2025-09-{10 + i:02d}T19:00:00-04:00", 1, 2, "final", 2 + (i % 3), 1 + (i % 3), None, None))
            games.append((400 + i, f"2025-09-{10 + i:02d}", f"2025-09-{10 + i:02d}T21:00:00-04:00", 3, 4, "final", 1 + (i % 4), 2 + (i % 2), None, None))
        for i in range(3):
            games.append((500 + i, f"2025-09-{20 + i:02d}", f"2025-09-{20 + i:02d}T19:00:00-04:00", 1, 3, "final", 2, 2, None, None))
            games.append((510 + i, f"2025-09-{23 + i:02d}", f"2025-09-{23 + i:02d}T19:00:00-04:00", 2, 3, "final", 2, 2, None, None))
            games.append((520 + i, f"2025-09-{26 + i:02d}", f"2025-09-{26 + i:02d}T21:00:00-04:00", 3, 1, "final", 2, 2, None, None))
            games.append((530 + i, f"2025-09-{29 + i:02d}", f"2025-09-{29 + i:02d}T21:00:00-04:00", 4, 1, "final", 2, 2, None, None))
        for i in range(10):
            games.append((600 + i, f"2025-08-{10 + i:02d}", f"2025-08-{10 + i:02d}T19:00:00-04:00", 3, 5, "final", 2, 2, None, None))
            games.append((700 + i, f"2025-08-{10 + i:02d}", f"2025-08-{10 + i:02d}T19:30:00-04:00", 10, 9, "final", 2, 2, None, None))
            games.append((800 + i, f"2025-08-{10 + i:02d}", f"2025-08-{10 + i:02d}T22:00:00-04:00", 12, 11, "final", 2, 2, None, None))
        conn.executemany(
            "INSERT INTO games (id, game_date, start_time, home_team_id, away_team_id, status, rest_days_home, rest_days_away, spread_home, game_total) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            games,
        )
        team_results = []
        lv_scores = [(86, 78), (91, 84), (77, 82), (88, 79), (93, 89), (80, 76), (72, 81), (95, 87), (84, 83), (90, 85)]
        ind_scores = [(78, 86), (84, 91), (82, 77), (79, 88), (89, 93), (76, 80), (81, 72), (87, 95), (83, 84), (85, 90)]
        ny_scores = [(82, 74), (88, 80), (79, 81), (91, 86), (85, 77), (78, 83), (94, 88), (87, 84), (76, 79), (90, 82)]
        min_scores = [(74, 82), (80, 88), (81, 79), (86, 91), (77, 85), (83, 78), (88, 94), (84, 87), (79, 76), (82, 90)]
        spreads = [-5.5, -4.5, -6.5, -3.5, -7.5, -2.5, -5.5, -6.5, -4.5, -5.5]
        totals = [164.5, 171.5, 160.5, 166.5, 178.5, 158.5, 154.5, 181.5, 168.5, 173.5]
        for i in range(10):
            game_id = 300 + i
            lv_pts, lv_opp = lv_scores[i]
            ind_pts, ind_opp = ind_scores[i]
            total_points = lv_pts + ind_pts
            lv_margin_vs_spread = (lv_pts - lv_opp) + spreads[i]
            ind_margin_vs_spread = (ind_pts - ind_opp) - spreads[i]
            possessions = 76 + (i % 5)
            team_results.append((None, 1, game_id, 1, lv_pts, lv_opp, possessions, spreads[i], totals[i], _ats(lv_margin_vs_spread), _total(total_points, totals[i])))
            team_results.append((None, 2, game_id, 0, ind_pts, ind_opp, possessions, -spreads[i], totals[i], _ats(ind_margin_vs_spread), _total(total_points, totals[i])))

            game_id = 400 + i
            ny_pts, ny_opp = ny_scores[i]
            min_pts, min_opp = min_scores[i]
            ny_spread = spreads[(i + 3) % len(spreads)]
            game_total = totals[(i + 2) % len(totals)]
            total_points = ny_pts + min_pts
            ny_margin_vs_spread = (ny_pts - ny_opp) + ny_spread
            min_margin_vs_spread = (min_pts - min_opp) - ny_spread
            possessions = 77 + ((i + 2) % 4)
            team_results.append((None, 3, game_id, 1, ny_pts, ny_opp, possessions, ny_spread, game_total, _ats(ny_margin_vs_spread), _total(total_points, game_total)))
            team_results.append((None, 4, game_id, 0, min_pts, min_opp, possessions, -ny_spread, game_total, _ats(min_margin_vs_spread), _total(total_points, game_total)))
        common_scores = [
            (500, 1, 3, 88, 82, -4.5, 168.5),
            (501, 1, 3, 84, 86, -3.5, 166.5),
            (502, 1, 3, 91, 87, -2.5, 171.5),
            (510, 2, 3, 79, 83, 4.5, 165.5),
            (511, 2, 3, 87, 85, 5.5, 170.5),
            (512, 2, 3, 82, 90, 3.5, 167.5),
            (520, 3, 1, 86, 89, 2.5, 170.5),
            (521, 3, 1, 80, 84, 1.5, 164.5),
            (522, 3, 1, 92, 88, 3.5, 174.5),
            (530, 4, 1, 78, 87, 4.5, 166.5),
            (531, 4, 1, 83, 85, 5.5, 169.5),
            (532, 4, 1, 81, 90, 3.5, 168.5),
        ]
        for game_id, home_team_id, away_team_id, home_pts, away_pts, home_spread, game_total in common_scores:
            total_points = home_pts + away_pts
            possessions = 78 + (game_id % 4)
            home_margin_vs_spread = (home_pts - away_pts) + home_spread
            away_margin_vs_spread = (away_pts - home_pts) - home_spread
            team_results.append((None, home_team_id, game_id, 1, home_pts, away_pts, possessions, home_spread, game_total, _ats(home_margin_vs_spread), _total(total_points, game_total)))
            team_results.append((None, away_team_id, game_id, 0, away_pts, home_pts, possessions, -home_spread, game_total, _ats(away_margin_vs_spread), _total(total_points, game_total)))
        current_slate_scores = [
            (600, 3, 5, 88, 79, -7.5, 163.5),
            (601, 3, 5, 91, 84, -6.5, 168.5),
            (602, 3, 5, 82, 80, -5.5, 160.5),
            (603, 3, 5, 94, 86, -8.5, 171.5),
            (604, 3, 5, 86, 83, -4.5, 165.5),
            (605, 3, 5, 89, 77, -9.5, 162.5),
            (606, 3, 5, 80, 78, -6.5, 158.5),
            (607, 3, 5, 92, 88, -5.5, 174.5),
            (608, 3, 5, 85, 81, -6.5, 164.5),
            (609, 3, 5, 90, 82, -7.5, 166.5),
            (700, 10, 9, 78, 81, 1.5, 158.5),
            (701, 10, 9, 84, 80, -1.5, 162.5),
            (702, 10, 9, 76, 79, 2.5, 155.5),
            (703, 10, 9, 87, 85, -2.5, 168.5),
            (704, 10, 9, 82, 77, -1.5, 159.5),
            (705, 10, 9, 79, 83, 1.5, 160.5),
            (706, 10, 9, 88, 84, -2.5, 169.5),
            (707, 10, 9, 81, 80, -1.5, 157.5),
            (708, 10, 9, 85, 86, 1.5, 166.5),
            (709, 10, 9, 83, 78, -2.5, 161.5),
            (800, 12, 11, 84, 76, -5.5, 158.5),
            (801, 12, 11, 79, 82, -2.5, 159.5),
            (802, 12, 11, 88, 80, -4.5, 165.5),
            (803, 12, 11, 81, 78, -3.5, 156.5),
            (804, 12, 11, 86, 84, -2.5, 168.5),
            (805, 12, 11, 90, 83, -5.5, 171.5),
            (806, 12, 11, 77, 75, -1.5, 151.5),
            (807, 12, 11, 85, 81, -4.5, 163.5),
            (808, 12, 11, 82, 79, -3.5, 160.5),
            (809, 12, 11, 89, 85, -2.5, 170.5),
        ]
        for game_id, home_team_id, away_team_id, home_pts, away_pts, home_spread, game_total in current_slate_scores:
            total_points = home_pts + away_pts
            possessions = 76 + (game_id % 6)
            home_margin_vs_spread = (home_pts - away_pts) + home_spread
            away_margin_vs_spread = (away_pts - home_pts) - home_spread
            team_results.append((None, home_team_id, game_id, 1, home_pts, away_pts, possessions, home_spread, game_total, _ats(home_margin_vs_spread), _total(total_points, game_total)))
            team_results.append((None, away_team_id, game_id, 0, away_pts, home_pts, possessions, -home_spread, game_total, _ats(away_margin_vs_spread), _total(total_points, game_total)))
        conn.executemany(
            """
            INSERT INTO team_game_results (
                id, team_id, game_id, is_home, points, opponent_points, possessions,
                closing_spread, closing_total, ats_result, total_result
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            team_results,
        )
        stat_rows = []
        wilson_points = [24, 28, 31, 22, 27, 30, 19, 25, 29, 26]
        clark_assists = [8, 10, 7, 12, 9, 6, 11, 8, 10, 9]
        stewart_reb = [9, 7, 11, 8, 10, 12, 6, 9, 8, 10]
        collier_pra = [(22, 8, 4), (25, 10, 3), (18, 9, 5), (24, 7, 4), (27, 11, 4), (20, 8, 6), (23, 10, 4), (21, 9, 3), (26, 12, 5), (19, 7, 4)]
        for i in range(10):
            lv_ind_game_id = 300 + i
            ny_min_game_id = 400 + i
            stat_rows.append((None, 101, lv_ind_game_id, 33, wilson_points[i], 10, 2, 1, 1, 2, 2))
            stat_rows.append((None, 102, lv_ind_game_id, 35, 19, 5, clark_assists[i], 4, 1, 0, 5))
            stat_rows.append((None, 103, ny_min_game_id, 32, 21, stewart_reb[i], 3, 2, 1, 1, 2))
            pts, reb, ast = collier_pra[i]
            stat_rows.append((None, 104, ny_min_game_id, 34, pts, reb, ast, 1, 2, 1, 2))
            stat_rows.extend(
                [
                    (None, 105, 600 + i, 31 + (i % 3), 15 + (i % 5), 4 + (i % 3), 5 + (i % 4), 2 + (i % 3), 1, 0, 2),
                    (None, 106, 600 + i, 33 + (i % 2), 18 + (i % 6), 5 + (i % 4), 4 + (i % 3), 2, 1, 0, 3),
                    (None, 107, 600 + i, 29 + (i % 3), 12 + (i % 4), 8 + (i % 5), 2 + (i % 2), 0, 1, 1, 2),
                    (None, 108, 700 + i, 32 + (i % 3), 17 + (i % 5), 4 + (i % 3), 6 + (i % 4), 2 + (i % 2), 1, 0, 3),
                    (None, 109, 700 + i, 30 + (i % 2), 14 + (i % 5), 5 + (i % 3), 3 + (i % 3), 1 + (i % 2), 1, 1, 2),
                    (None, 110, 700 + i, 33 + (i % 2), 19 + (i % 6), 4 + (i % 4), 5 + (i % 3), 2 + (i % 3), 1, 0, 3),
                    (None, 111, 700 + i, 28 + (i % 3), 13 + (i % 4), 8 + (i % 4), 2 + (i % 2), 0, 1, 1, 2),
                    (None, 112, 800 + i, 32 + (i % 3), 16 + (i % 6), 4 + (i % 3), 6 + (i % 3), 2 + (i % 2), 1, 0, 3),
                    (None, 113, 800 + i, 30 + (i % 3), 14 + (i % 5), 5 + (i % 4), 3 + (i % 3), 1 + (i % 2), 1, 1, 2),
                    (None, 114, 800 + i, 33 + (i % 2), 18 + (i % 5), 4 + (i % 3), 5 + (i % 4), 2 + (i % 2), 1, 0, 3),
                    (None, 115, 800 + i, 29 + (i % 3), 12 + (i % 4), 9 + (i % 5), 2 + (i % 2), 0, 1, 1, 2),
                ]
            )
        extra_stats = [
            (101, 500, 34, 29, 11, 3, 1, 1, 2, 2),
            (101, 501, 32, 23, 9, 2, 1, 1, 1, 2),
            (101, 502, 35, 31, 12, 2, 2, 1, 2, 1),
            (102, 510, 36, 21, 5, 9, 4, 1, 0, 4),
            (102, 511, 35, 24, 6, 11, 5, 2, 0, 5),
            (102, 512, 34, 18, 4, 7, 3, 1, 0, 4),
            (103, 520, 33, 22, 10, 3, 2, 1, 1, 2),
            (103, 521, 32, 20, 8, 4, 2, 1, 1, 2),
            (103, 522, 34, 25, 11, 3, 3, 1, 2, 2),
            (104, 530, 35, 24, 10, 5, 1, 2, 1, 2),
            (104, 531, 34, 22, 9, 4, 1, 1, 1, 2),
            (104, 532, 35, 26, 11, 5, 2, 2, 1, 3),
        ]
        stat_rows.extend((None, *row) for row in extra_stats)
        conn.executemany(
            """
            INSERT INTO player_game_stats (
                id, player_id, game_id, minutes, points, rebounds, assists, threes, steals, blocks, turnovers
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            stat_rows,
        )
        conn.executemany(
            """
            INSERT INTO prop_lines (
                id, game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (521, 700, 109, "BetMGM", "points", 14.5, -110, -110, captured_at),
                (522, 700, 109, "Caesars", "points_rebounds_assists", 22.5, -115, -105, captured_at),
                (523, 700, 110, "DraftKings", "points", 19.5, -110, -110, captured_at),
                (524, 700, 110, "FanDuel", "assists", 5.5, -105, -115, captured_at),
                (525, 700, 111, "BetMGM", "rebounds", 8.5, -110, -110, captured_at),
                (526, 700, 111, "Caesars", "points_rebounds_assists", 23.5, -115, -105, captured_at),
                (527, 800, 112, "DraftKings", "points", 16.5, -110, -110, captured_at),
                (528, 800, 112, "FanDuel", "assists", 5.5, -105, -115, captured_at),
                (529, 800, 113, "BetMGM", "points_rebounds_assists", 22.5, -110, -110, captured_at),
                (530, 800, 113, "Caesars", "rebounds", 5.5, -115, -105, captured_at),
                (531, 800, 114, "DraftKings", "points", 18.5, -110, -110, captured_at),
                (532, 800, 114, "FanDuel", "points_rebounds_assists", 28.5, -105, -115, captured_at),
                (533, 800, 115, "BetMGM", "rebounds", 9.5, -110, -110, captured_at),
                (534, 800, 115, "Caesars", "points_rebounds_assists", 23.5, -115, -105, captured_at),
            ],
        )
        conn.execute(
            """
            INSERT INTO manual_adjustments (player_id, projected_minutes, usage_multiplier, note)
            VALUES (?, ?, ?, ?)
            """,
            (101, 34, 1.04, "expected full workload"),
        )
        rebuild_predictions(conn)

def _ats(margin_vs_spread: float) -> str:
    if margin_vs_spread > 0:
        return "cover"
    if margin_vs_spread < 0:
        return "no_cover"
    return "push"


def _total(total_points: int, closing_total: float) -> str:
    if total_points > closing_total:
        return "over"
    if total_points < closing_total:
        return "under"
    return "push"


if __name__ == "__main__":
    seed_sample_data()
