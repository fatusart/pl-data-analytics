"""Tendances de forme : fenêtres glissantes, moyenne exponentielle, séries, splits domicile/extérieur."""
from __future__ import annotations

import pandas as pd

from skills.registry import skill


@skill("analysis.team_form", "analysis",
       "Forme roulante par équipe : PPG, buts, xG, EWMA et chaîne 'WWDLW'")
def team_form(team_matches: pd.DataFrame, window: int = 5, halflife: float = 4.0,
              across_seasons: bool = False) -> pd.DataFrame:
    """`across_seasons=True` : la forme ne repart pas de zéro en août (utile pour la prédiction)."""
    keys = ["team_id"] if across_seasons else ["season_id", "team_id"]
    df = team_matches.sort_values(keys + ["match_date", "match_id"]).copy()
    g = df.groupby(keys, sort=False)

    roll = lambda col: g[col].transform(lambda s: s.rolling(window, min_periods=1).mean())  # noqa: E731
    df[f"ppg_last{window}"] = roll("points").round(2)
    df[f"gf_last{window}"] = roll("goals_for").round(2)
    df[f"ga_last{window}"] = roll("goals_against").round(2)
    if "xg_for" in df:
        df["xgd"] = df["xg_for"] - df["xg_against"]
        df[f"xgd_last{window}"] = roll("xgd").round(2)
    df["ppg_ewm"] = g["points"].transform(lambda s: s.ewm(halflife=halflife).mean()).round(2)
    # Chaîne de forme (rolling ne gère pas les str => construction explicite)
    df["form_string"] = g["result"].transform(
        lambda s: pd.Series(["".join(s.iloc[max(0, i - window + 1): i + 1]) for i in range(len(s))],
                            index=s.index))
    # Momentum : forme récente vs moyenne de saison (> 0 = en progression)
    df["season_ppg_to_date"] = g["points"].transform(lambda s: s.expanding().mean()).round(2)
    df["momentum"] = (df["ppg_ewm"] - df["season_ppg_to_date"]).round(2)
    return df


@skill("analysis.streaks", "analysis", "Séries en cours (victoires, invincibilité, clean sheets)")
def current_streaks(team_matches: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (season, team), grp in team_matches.sort_values("match_date").groupby(["season_id", "team_name"]):
        res = grp["result"].tolist()[::-1]
        ga = grp["goals_against"].tolist()[::-1]

        def run(pred, seq):
            n = 0
            for x in seq:
                if not pred(x):
                    break
                n += 1
            return n

        rows.append({"season_id": season, "team_name": team,
                     "win_streak": run(lambda r: r == "W", res),
                     "unbeaten": run(lambda r: r != "L", res),
                     "winless": run(lambda r: r != "W", res),
                     "clean_sheets": run(lambda g: g == 0, ga)})
    return pd.DataFrame(rows)


@skill("analysis.home_away", "analysis", "Splits domicile / extérieur par équipe et saison")
def home_away_splits(team_matches: pd.DataFrame) -> pd.DataFrame:
    out = team_matches.groupby(["season_id", "team_name", "is_home"]).agg(
        played=("match_id", "count"), ppg=("points", "mean"),
        gf=("goals_for", "mean"), ga=("goals_against", "mean"),
    ).round(2).reset_index()
    out["venue"] = out["is_home"].map({True: "Domicile", False: "Extérieur"})
    return out.drop(columns="is_home")
