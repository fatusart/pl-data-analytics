"""Métriques FPL avancées (grain joueur, à une gameweek donnée)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from skills.registry import skill

# Joueur "de remplacement" = le N-ième meilleur du poste (≈ ce qu'un manager obtient pour pas cher)
REPLACEMENT_RANK = {"GKP": 20, "DEF": 60, "MID": 60, "FWD": 30}


@skill("analysis.fpl_player_metrics", "analysis",
       "Forme, points/£m, xGI/90, sécurité de temps de jeu, VORP, momentum de transferts")
def player_metrics(gw: pd.DataFrame, as_of_gw: int, window: int = 5, min_minutes: int = 270) -> pd.DataFrame:
    """`gw` : v_fpl_player_gameweek d'une saison (1 ligne joueur × GW)."""
    hist = gw[gw["gw_number"] <= as_of_gw]
    recent = hist[hist["gw_number"] > as_of_gw - window]

    season = hist.groupby(["player_id", "web_name", "position"], dropna=False).agg(
        team=("team", "last"), price=("price", "last"), points=("points", "sum"),
        minutes=("minutes", "sum"), xgi=("xgi", "sum"), selected_by=("selected_by", "last"),
        gws_played=("minutes", lambda m: int((m > 0).sum())),
    )
    form = recent.groupby("player_id").agg(
        form=("points", "mean"), recent_minutes=("minutes", "sum"),
        transfers_balance=("transfers_balance", "sum"))
    df = season.reset_index().join(form, on="player_id")

    df["points_per_million"] = (df["points"] / df["price"]).round(2)
    df["points_per_90"] = np.where(df["minutes"] > 0, df["points"] / df["minutes"] * 90, np.nan).round(2)
    df["xgi_per_90"] = np.where(df["minutes"] > 0, df["xgi"] / df["minutes"] * 90, np.nan).round(2)
    # Sécurité de temps de jeu : part des minutes possibles sur la fenêtre récente
    df["minutes_security"] = (df["recent_minutes"] / (90 * window)).clip(0, 1).round(2)
    # Momentum de transferts normalisé par la base de propriétaires (évite le biais des stars)
    df["transfer_momentum"] = (df["transfers_balance"] / df["selected_by"].clip(lower=1000)).round(3)
    df["form"] = df["form"].round(2)

    # VORP : points au-dessus du joueur de remplacement du même poste
    df["vorp"] = np.nan
    for pos, rank in REPLACEMENT_RANK.items():
        mask = df["position"] == pos
        if mask.any():
            pts = df.loc[mask, "points"].sort_values(ascending=False)
            repl = pts.iloc[min(rank, len(pts)) - 1]
            df.loc[mask, "vorp"] = df.loc[mask, "points"] - repl
    df["eligible"] = df["minutes"] >= min_minutes
    return df.sort_values("points", ascending=False).reset_index(drop=True)


@skill("analysis.fixture_difficulty", "analysis",
       "Difficulté des N prochaines fixtures par équipe (FDR modélisé, 1 = facile … 5 = difficile)")
def fixture_difficulty(upcoming: pd.DataFrame, strengths,
                       horizon: int = 5) -> tuple[pd.DataFrame, pd.DataFrame]:
    """`upcoming` : fixtures à venir (home_team, away_team, gw_number) ; `strengths` : TeamStrengths.

    La difficulté = xG concédé attendu − xG marqué attendu, recalée en quintiles 1..5
    (plus fin que le FDR officiel FPL qui est statique et discret).
    Retourne (détail par fixture, synthèse par équipe sur l'horizon).
    """
    rows = []
    for r in upcoming.itertuples():
        xh, xa = strengths.expected_goals(r.home_team, r.away_team)
        rows.append({"team": r.home_team, "opponent": r.away_team, "gw_number": r.gw_number,
                     "is_home": True, "xg_for": xh, "xg_against": xa})
        rows.append({"team": r.away_team, "opponent": r.home_team, "gw_number": r.gw_number,
                     "is_home": False, "xg_for": xa, "xg_against": xh})
    df = pd.DataFrame(rows)
    if df.empty:
        return df, df
    df["difficulty_raw"] = df["xg_against"] - df["xg_for"]
    df["fdr"] = pd.qcut(df["difficulty_raw"].rank(method="first"), 5, labels=[1, 2, 3, 4, 5]).astype(int)
    first_gws = sorted(df["gw_number"].dropna().unique())[:horizon]
    window = df[df["gw_number"].isin(first_gws)]
    summary = window.groupby("team").agg(avg_fdr=("fdr", "mean"), fixtures=("fdr", "size"),
                                         exp_goals_for=("xg_for", "sum"),
                                         exp_clean_sheet_prob=("xg_against", lambda x: np.exp(-x).mean()))
    return df, summary.round(2).sort_values("avg_fdr").reset_index()


@skill("analysis.captaincy", "analysis", "Score de capitanat = forme × xGI/90 × facilité de la prochaine fixture")
def captaincy_scores(metrics: pd.DataFrame, next_fixture: pd.DataFrame) -> pd.DataFrame:
    """`next_fixture` : sortie détaillée de fixture_difficulty filtrée sur la prochaine GW."""
    nxt = next_fixture.groupby("team").agg(team_xg=("xg_for", "sum"), fdr=("fdr", "mean"))
    df = metrics[metrics["eligible"]].join(nxt, on="team")
    df["captain_score"] = (
        0.4 * df["form"].rank(pct=True)
        + 0.3 * df["xgi_per_90"].rank(pct=True)
        + 0.2 * df["team_xg"].rank(pct=True)
        + 0.1 * df["minutes_security"]
    ).round(3)
    return df.sort_values("captain_score", ascending=False)
