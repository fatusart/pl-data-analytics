"""Marché des paris : probabilités implicites, marge bookmaker, calibration modèle vs marché.

Usage analytique uniquement (évaluation de modèles) — pas de conseil de pari.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from skills.registry import skill


@skill("analysis.implied_probs", "analysis", "Probabilités implicites 1X2 sans marge + overround")
def implied_probabilities(matches: pd.DataFrame, prefix: str = "odds_avg") -> pd.DataFrame:
    df = matches.copy()
    inv = pd.DataFrame({k: 1 / df[f"{prefix}_{k}"] for k in ("h", "d", "a")})
    df["overround"] = (inv.sum(axis=1) - 1).round(4)
    # Normalisation proportionnelle (simple et robuste ; la méthode de Shin corrige le biais favori/outsider)
    for k, col in (("h", "mkt_p_home"), ("d", "mkt_p_draw"), ("a", "mkt_p_away")):
        df[col] = (inv[k] / inv.sum(axis=1)).round(4)
    return df


def _outcome_matrix(ftr: pd.Series) -> np.ndarray:
    return np.column_stack([(ftr == "H"), (ftr == "D"), (ftr == "A")]).astype(float)


@skill("analysis.calibration", "analysis", "Brier score et log-loss : modèle vs marché")
def compare_to_market(df: pd.DataFrame, model_cols=("p_home", "p_draw", "p_away"),
                      market_cols=("mkt_p_home", "mkt_p_draw", "mkt_p_away")) -> pd.DataFrame:
    df = df.dropna(subset=[*model_cols, *market_cols, "ftr"])
    y = _outcome_matrix(df["ftr"])
    out = {}
    for name, cols in (("modèle", model_cols), ("marché", market_cols)):
        p = np.clip(df[list(cols)].to_numpy(dtype=float), 1e-9, 1)
        out[name] = {"brier": float(((p - y) ** 2).sum(axis=1).mean()),
                     "log_loss": float(-(y * np.log(p)).sum(axis=1).mean()),
                     "n": len(df)}
    return pd.DataFrame(out).T.round(4)


@skill("analysis.home_advantage_trend", "analysis", "Évolution de l'avantage domicile par saison (effet huis clos 2020-21)")
def home_advantage_by_season(matches: pd.DataFrame) -> pd.DataFrame:
    played = matches.dropna(subset=["ftr"])
    return played.groupby("season_id").agg(
        home_win=("ftr", lambda r: (r == "H").mean()),
        draw=("ftr", lambda r: (r == "D").mean()),
        away_win=("ftr", lambda r: (r == "A").mean()),
        goals_per_match=("fthg", lambda s: (s + played.loc[s.index, "ftag"]).mean()),
    ).round(3).reset_index()
