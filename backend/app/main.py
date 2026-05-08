from __future__ import annotations

from datetime import datetime

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .ball_dont_lie import fetch_team_history
from .bootstrap import ensure_teams
from .cache import write_json_cache
from .db import connect, init_db
from .espn_history import import_espn_player_boxscores, import_espn_scoreboard
from .game_predictions import project_game
from .odds_import import import_the_odds_api_props, line_discrepancies, list_sportsbook_props, odds_cache_summary, sync_prop_lines_from_sportsbook
from .projections import rebuild_predictions
from .sportsdataio import import_sportsdataio
from .training import latest_model_run, list_model_runs, run_walk_forward_training


app = FastAPI(title="WNBA Prop Value API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    with connect() as conn:
        ensure_teams(conn)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/recalculate")
def recalculate() -> dict[str, int]:
    with connect() as conn:
        projections = rebuild_predictions(conn)
    return {"predictions": len(projections)}


@app.get("/api/value-board")
def value_board() -> list[dict]:
    with connect() as conn:
        payload = _value_board_payload(conn)
    write_json_cache("current_value_board.json", payload)
    return payload


@app.get("/api/model-performance")
def model_performance() -> dict:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                pp.recommended_side,
                pp.expected_value,
                sp.winning_side
            FROM prop_predictions pp
            JOIN settled_props sp ON sp.prop_line_id = pp.prop_line_id
            """
        ).fetchall()
    if not rows:
        return {
            "settled": 0,
            "wins": 0,
            "win_rate": None,
            "average_ev": None,
            "message": "No settled props yet. Settle completed games to evaluate the model.",
        }
    wins = sum(1 for row in rows if row["recommended_side"] == row["winning_side"])
    avg_ev = sum(float(row["expected_value"]) for row in rows) / len(rows)
    return {
        "settled": len(rows),
        "wins": wins,
        "win_rate": round(wins / len(rows), 4),
        "average_ev": round(avg_ev, 4),
    }


@app.post("/api/models/train")
def train_model() -> dict:
    with connect() as conn:
        return run_walk_forward_training(conn)


@app.post("/api/odds/import")
def import_odds(force_refresh: bool = False) -> dict:
    with connect() as conn:
        result = import_the_odds_api_props(conn, force_refresh=force_refresh)
    return result


@app.post("/api/history/import/espn")
def import_espn_history(
    season: int | None = None,
    force_refresh: bool = True,
    include_player_stats: bool = True,
    include_previous_season: bool = True,
) -> dict:
    target_season = season or datetime.now().year
    seasons = [target_season - 1, target_season] if include_previous_season else [target_season]
    unique_seasons = sorted(set(seasons))
    with connect() as conn:
        scoreboards = [import_espn_scoreboard(conn, item, force_refresh=force_refresh) for item in unique_seasons]
        player_stats = []
        if include_player_stats:
            player_stats = [import_espn_player_boxscores(conn, item, force_refresh=force_refresh) for item in unique_seasons]
        synced_props = sync_prop_lines_from_sportsbook(conn)
    return {
        "season": target_season,
        "seasons": unique_seasons,
        "scoreboards": scoreboards,
        "player_stats": player_stats,
        "synced_props": synced_props,
        "source": "espn",
    }


@app.post("/api/sportsdataio/import")
def import_sportsdataio_data(
    season: int | None = None,
    force_refresh: bool = False,
    include_boxscores: bool = True,
    include_odds: bool = True,
) -> dict:
    with connect() as conn:
        return import_sportsdataio(
            conn,
            season=season,
            force_refresh=force_refresh,
            include_boxscores=include_boxscores,
            include_odds=include_odds,
        )


@app.get("/api/sportsbook-props")
def sportsbook_props(game_id: int | None = None) -> list[dict]:
    with connect() as conn:
        payload = list_sportsbook_props(conn, game_id)
    write_json_cache("sportsbook_props.json", payload)
    return payload


@app.get("/api/odds/cache")
def odds_cache() -> dict:
    return odds_cache_summary()


@app.get("/api/line-discrepancies")
def discrepancies(game_id: int | None = None) -> list[dict]:
    with connect() as conn:
        payload = line_discrepancies(conn, game_id)
    write_json_cache("line_discrepancies.json", payload)
    return payload


@app.get("/api/models/runs")
def model_runs() -> dict:
    with connect() as conn:
        return {
            "latest": latest_model_run(conn),
            "runs": list_model_runs(conn),
        }


@app.get("/api/ball_dont_lie/history")
def ball_dont_lie_history(
    team: str,
    seasons: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    force_refresh: bool = False,
) -> dict:
    season_list: list[int] | None = None
    if seasons:
        try:
            season_list = [int(value.strip()) for value in seasons.split(",") if value.strip()]
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid seasons value: {exc}")

    try:
        return fetch_team_history(
            team=team,
            seasons=season_list,
            start_date=start_date,
            end_date=end_date,
            force_refresh=force_refresh,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/matchups")
def matchups() -> list[dict]:
    with connect() as conn:
        games = conn.execute(
            """
            SELECT
                g.id,
                g.game_date,
                g.start_time,
                home.id AS home_team_id,
                home.abbreviation AS home_team,
                home.name AS home_team_name,
                home.logo_url AS home_logo_url,
                away.id AS away_team_id,
                away.abbreviation AS away_team,
                away.name AS away_team_name
                ,away.logo_url AS away_logo_url
                ,g.rest_days_home
                ,g.rest_days_away
                ,g.spread_home
                ,g.game_total
            FROM games g
            JOIN teams home ON home.id = g.home_team_id
            JOIN teams away ON away.id = g.away_team_id
            WHERE g.status = 'scheduled'
              AND EXISTS (
                  SELECT 1
                  FROM sportsbook_prop_lines spl
                  WHERE spl.game_id = g.id
              )
            ORDER BY g.start_time
            """
        ).fetchall()
        payload = []
        for game in games:
            home_summary = _team_last_10_summary(conn, int(game["home_team_id"]))
            away_summary = _team_last_10_summary(conn, int(game["away_team_id"]))
            game_id = int(game["id"])
            home_rest_days = _rest_days_before_game(conn, int(game["home_team_id"]), game["game_date"])
            away_rest_days = _rest_days_before_game(conn, int(game["away_team_id"]), game["game_date"])
            game_context = dict(game)
            game_context["rest_days_home"] = home_rest_days if home_rest_days is not None else 2
            game_context["rest_days_away"] = away_rest_days if away_rest_days is not None else 2
            prediction = project_game(conn, game_context)
            payload.append(
                {
                    "id": game["id"],
                    "game_date": game["game_date"],
                    "start_time": game["start_time"],
                    "home_team": game["home_team"],
                    "home_team_name": game["home_team_name"],
                    "home_logo_url": game["home_logo_url"],
                    "away_team": game["away_team"],
                    "away_team_name": game["away_team_name"],
                    "away_logo_url": game["away_logo_url"],
                    "home_rest_days": home_rest_days,
                    "away_rest_days": away_rest_days,
                    "spread_home": game["spread_home"],
                    "game_total": game["game_total"],
                    "blowout_risk": _blowout_display(game["spread_home"], "starter")["blowout_risk"],
                    **prediction,
                    "home": home_summary,
                    "away": away_summary,
                    "props": _value_board_payload(conn, game_id),
                    "sportsbook_props": list_sportsbook_props(conn, game_id),
                    "line_discrepancies": line_discrepancies(conn, game_id),
                }
            )
    write_json_cache("current_matchups.json", payload)
    return payload


def _value_board_payload(conn, game_id: int | None = None) -> list[dict]:
    game_filter = "WHERE pl.game_id = ?" if game_id is not None else ""
    params = (game_id,) if game_id is not None else ()
    rows = conn.execute(
        f"""
        SELECT
            pp.id,
            pl.game_id,
            p.full_name AS player,
            t.abbreviation AS team,
            t.logo_url AS team_logo_url,
            pl.sportsbook,
            pl.market,
            pl.line,
            pl.over_odds,
            pl.under_odds,
            pp.projection,
            pp.recommended_side,
            pp.model_probability,
            pp.implied_probability,
            pp.edge,
            pp.expected_value,
            pp.confidence,
            pp.reason,
            pp.prediction_time,
            g.start_time,
            CASE
                WHEN p.team_id = g.home_team_id THEN g.rest_days_home
                ELSE g.rest_days_away
            END AS rest_days,
            p.rotation_role,
            g.spread_home,
            g.game_total,
            CASE
                WHEN p.team_id = g.home_team_id THEN g.spread_home
                ELSE -g.spread_home
            END AS team_spread
        FROM prop_predictions pp
        JOIN prop_lines pl ON pl.id = pp.prop_line_id
        JOIN players p ON p.id = pl.player_id
        JOIN teams t ON t.id = p.team_id
        JOIN games g ON g.id = pl.game_id
        {game_filter}
        ORDER BY pp.expected_value DESC, pp.edge DESC
        """,
        params,
    ).fetchall()
    payload = []
    for row in rows:
        item = dict(row)
        item.update(_blowout_display(item["team_spread"], item["rotation_role"]))
        payload.append(item)
    return payload


def _blowout_display(team_spread, role: str | None) -> dict:
    if team_spread is None:
        return {
            "blowout_risk": "unknown",
            "blowout_probability": 0.0,
            "blowout_minutes_impact": 0.0,
        }
    spread_abs = abs(float(team_spread))
    probability = _blowout_probability(spread_abs)
    role_delta = {
        "star": -4.0,
        "starter": -3.0,
        "rotation": -1.5,
        "bench": 2.0,
    }.get(role or "starter", -2.0)
    return {
        "blowout_risk": _blowout_label(probability),
        "blowout_probability": round(probability, 2),
        "blowout_minutes_impact": round(probability * role_delta, 1),
    }


def _blowout_probability(spread_abs: float) -> float:
    if spread_abs < 4.5:
        return 0.08
    if spread_abs < 8.5:
        return 0.18
    if spread_abs < 12.5:
        return 0.34
    if spread_abs < 16.5:
        return 0.48
    return 0.60


def _blowout_label(probability: float) -> str:
    if probability >= 0.45:
        return "very high"
    if probability >= 0.30:
        return "high"
    if probability >= 0.15:
        return "medium"
    return "low"


def _team_last_10_summary(conn, team_id: int) -> dict:
    rows = conn.execute(
        """
        SELECT
            r.*,
            g.game_date,
            g.start_time,
            opponent.abbreviation AS opponent
        FROM team_game_results r
        JOIN games g ON g.id = r.game_id
        JOIN teams opponent ON opponent.id = CASE
            WHEN g.home_team_id = r.team_id THEN g.away_team_id
            ELSE g.home_team_id
        END
        WHERE r.team_id = ?
        ORDER BY g.game_date DESC, g.start_time DESC
        LIMIT 10
        """,
        (team_id,),
    ).fetchall()

    recent_games = [dict(row) for row in rows]
    count = len(rows)
    wins = 0
    ats_wins = 0
    ats_losses = 0
    ats_pushes = 0
    overs = 0
    unders = 0
    total_pushes = 0
    home_games = 0
    total_pts = 0
    total_opp_pts = 0

    for row in rows:
        points = row["points"]
        opponent_points = row["opponent_points"]

        if points > opponent_points:
            wins += 1
        if row["is_home"]:
            home_games += 1

        if row["ats_result"] == "cover":
            ats_wins += 1
        elif row["ats_result"] == "no_cover":
            ats_losses += 1
        else:
            ats_pushes += 1

        if row["total_result"] == "over":
            overs += 1
        elif row["total_result"] == "under":
            unders += 1
        else:
            total_pushes += 1

        total_pts += points
        total_opp_pts += opponent_points

    return {
        "games": count,
        "wins": wins,
        "losses": count - wins,
        "home_games": home_games,
        "away_games": count - home_games,
        "ats_wins": ats_wins,
        "ats_losses": ats_losses,
        "ats_pushes": ats_pushes,
        "overs": overs,
        "unders": unders,
        "total_pushes": total_pushes,
        "avg_points_for": round(total_pts / count, 1) if count > 0 else 0,
        "avg_points_against": round(total_opp_pts / count, 1) if count > 0 else 0,
        "recent_games": recent_games,
    }


def _rest_days_before_game(conn, team_id: int, game_date: str) -> int | None:
    current = _parse_game_date(game_date)
    if current is None:
        return None
    row = conn.execute(
        """
        SELECT MAX(g.game_date) AS last_game_date
        FROM team_game_results r
        JOIN games g ON g.id = r.game_id
        WHERE r.team_id = ?
          AND g.status = 'final'
          AND g.game_date < ?
        """,
        (team_id, game_date),
    ).fetchone()
    if not row or not row["last_game_date"]:
        return None
    previous = _parse_game_date(str(row["last_game_date"]))
    if previous is None:
        return None
    rest_days = max((current - previous).days - 1, 0)
    if rest_days > 14:
        return None
    return rest_days


def _parse_game_date(value: str):
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        return None
