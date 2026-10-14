"""Classements roulants : position de chaque équipe après N matchs joués, ou à une date donnée."""
from __future__ import annotations

import pandas as pd

from skills.registry import skill

TIEBREAK = ["points", "goal_diff", "goals_for"]   # règles officielles PL (hors confrontations directes)


def _cumulate(team_matches: pd.DataFrame) -> pd.DataFrame:
    df = team_matches.sort_values(["season_id", "team_id", "match_date", "match_id"]).copy()
    g = df.groupby(["season_id", "team_id"], sort=False)
    df["matchday"] = g.cumcount() + 1
    df["points_cum"] = g["points"].cumsum()
    df["gf_cum"] = g["goals_for"].cumsum()
    df["ga_cum"] = g["goals_against"].cumsum()
    df["gd_cum"] = df["gf_cum"] - df["ga_cum"]
    return df


def _rank(frame: pd.DataFrame, group: list[str]) -> pd.Series:
    """Rang dense selon (points, différence, buts marqués) via tri lexicographique."""
    ordered = frame.sort_values(group + ["points", "goal_diff", "goals_for"],
                                ascending=[True] * len(group) + [False, False, False])
    key = ordered[["points", "goal_diff", "goals_for"]].apply(tuple, axis=1)
    pos = ordered.groupby(group, sort=False).cumcount() + 1
    # Égalité parfaite => même rang
    tie = key.eq(key.shift()) & ordered[group].eq(ordered[group].shift()).all(axis=1)
    pos = pos.where(~tie).ffill().astype(int)
    return pos.reindex(frame.index)


@skill("analysis.rolling_table", "analysis",
       "Position de chaque équipe après chaque journée (N matchs joués) — 'bump chart'")
def rolling_table_by_matchday(team_matches: pd.DataFrame) -> pd.DataFrame:
    cum = _cumulate(team_matches)
    snap = cum[["season_id", "team_id", "team_name", "short_name", "matchday",
                "points_cum", "gd_cum", "gf_cum"]].rename(
        columns={"points_cum": "points", "gd_cum": "goal_diff", "gf_cum": "goals_for"})
    snap["position"] = _rank(snap, ["season_id", "matchday"])
    return snap.sort_values(["season_id", "matchday", "position"]).reset_index(drop=True)


@skill("analysis.table_at_date", "analysis", "Classement officiel à une date donnée")
def table_at_date(team_matches: pd.DataFrame, season_id: int, as_of: pd.Timestamp | str) -> pd.DataFrame:
    df = team_matches[(team_matches["season_id"] == season_id)
                      & (team_matches["match_date"] <= pd.Timestamp(as_of))]
    table = df.groupby(["team_id", "team_name", "short_name"]).agg(
        played=("match_id", "count"),
        won=("result", lambda r: (r == "W").sum()),
        drawn=("result", lambda r: (r == "D").sum()),
        lost=("result", lambda r: (r == "L").sum()),
        goals_for=("goals_for", "sum"),
        goals_against=("goals_against", "sum"),
        points=("points", "sum"),
        xg_for=("xg_for", "sum"),
        xg_against=("xg_against", "sum"),
    ).reset_index()
    table["goal_diff"] = table["goals_for"] - table["goals_against"]
    table["season_id"] = season_id
    table["position"] = _rank(table, ["season_id"])
    table["ppg"] = (table["points"] / table["played"]).round(2)
    # Points "attendus" : surperformance / sous-performance vs xG (indicateur de régression)
    table["xg_diff"] = (table["xg_for"] - table["xg_against"]).round(1)
    return table.sort_values("position").reset_index(drop=True)
