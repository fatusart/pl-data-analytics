"""Requêtes de lecture partagées par les modules d'analyse et le dashboard."""
from __future__ import annotations

import pandas as pd

from core.db import read_sql


def team_matches(season_ids: list[int] | None = None) -> pd.DataFrame:
    """Vue équipe × match (v_team_match) enrichie des noms d'équipes."""
    where = "WHERE tm.season_id = ANY(:seasons)" if season_ids else ""
    df = read_sql(f"""
        SELECT tm.*, d.calendar_date AS match_date, t.team_name, t.short_name,
               o.team_name AS opponent_name, o.short_name AS opponent_short
        FROM pl.v_team_match tm
        JOIN pl.dim_dates d ON d.date_id = tm.date_id
        JOIN pl.dim_teams t ON t.team_id = tm.team_id
        JOIN pl.dim_teams o ON o.team_id = tm.opponent_id
        {where}
        ORDER BY tm.season_id, d.calendar_date, tm.match_id""", {"seasons": season_ids} if season_ids else None)
    df["match_date"] = pd.to_datetime(df["match_date"])
    return df


def matches(season_ids: list[int] | None = None, played: bool | None = True) -> pd.DataFrame:
    clauses = []
    if season_ids:
        clauses.append("m.season_id = ANY(:seasons)")
    if played is not None:
        clauses.append("m.is_played = :played")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    df = read_sql(f"""
        SELECT m.*, d.calendar_date AS match_date, ht.team_name AS home_team, at.team_name AS away_team,
               r.referee_name, g.gw_number
        FROM pl.fact_matches m
        JOIN pl.dim_dates d  ON d.date_id = m.date_id
        JOIN pl.dim_teams ht ON ht.team_id = m.home_team_id
        JOIN pl.dim_teams at ON at.team_id = m.away_team_id
        LEFT JOIN pl.dim_referees r ON r.referee_id = m.referee_id
        LEFT JOIN pl.dim_gameweeks g ON g.gameweek_id = m.gameweek_id
        {where}
        ORDER BY d.calendar_date, m.match_id""", {"seasons": season_ids, "played": played})
    df["match_date"] = pd.to_datetime(df["match_date"])
    return df


def fpl_player_gameweeks(season_id: int) -> pd.DataFrame:
    return read_sql("SELECT * FROM pl.v_fpl_player_gameweek WHERE season_id = :s ORDER BY player_id, gw_number",
                    {"s": season_id})


def latest_snapshot(season_id: int) -> pd.DataFrame:
    return read_sql("""
        SELECT s.*, p.web_name, ps.position, t.short_name AS team
        FROM pl.fact_fpl_player_snapshot s
        JOIN pl.dim_players p USING (player_id)
        LEFT JOIN pl.dim_player_season ps ON ps.player_id = s.player_id AND ps.season_id = s.season_id
        LEFT JOIN pl.dim_teams t ON t.team_id = ps.team_id
        WHERE s.season_id = :s
          AND s.snapshot_date = (SELECT MAX(snapshot_date) FROM pl.fact_fpl_player_snapshot WHERE season_id = :s)
    """, {"s": season_id})
