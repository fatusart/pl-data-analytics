"""Modélisation : xG proxy et modèle de force d'équipes (Poisson à décroissance temporelle).

1. xG proxy — football-data ne fournit pas de données tir par tir. On estime un xG
   au niveau match à partir des tirs et tirs cadrés :
     - si assez de matchs disposent d'un xG "réel" (FPL, 2022-23+), on calibre une
       régression (xG ~ tirs + tirs cadrés + domicile) sur ce xG réel ;
     - sinon, régression de Poisson buts ~ tirs + tirs cadrés.
   => permet une série xG homogène sur 10 ans (étiquetée "proxy" dans le dashboard).

2. Forces d'équipes — log(λ) = attaque_i − défense_j + avantage_domicile, pondéré par
   une décroissance exponentielle (matchs récents plus influents). Les probabilités
   1X2 se déduisent de la matrice de scores Poisson indépendante.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.stats import poisson
from sklearn.linear_model import LinearRegression, PoissonRegressor

from skills.registry import skill

log = logging.getLogger(__name__)
FEATURES = ["shots_for", "sot_for", "is_home"]
FIRST_REAL_XG_SEASON = 2022


@skill("analysis.xg_proxy", "analysis", "Estime un xG proxy (tirs/tirs cadrés) pour les saisons sans xG")
def fit_xg_proxy(team_matches: pd.DataFrame, min_real_xg_rows: int = 500) -> pd.DataFrame:
    df = team_matches.dropna(subset=["shots_for", "sot_for"]).copy()
    df["is_home"] = df["is_home"].astype(int)
    X = df[FEATURES].astype(float)

    # xG FPL disponible depuis 2022-23 (v_team_match privilégie le xG réel au proxy)
    has_real = df["xg_for"].notna() & (df["season_id"] >= FIRST_REAL_XG_SEASON)
    if has_real.sum() >= min_real_xg_rows:
        model = LinearRegression(positive=True).fit(X[has_real], df.loc[has_real, "xg_for"])
        log.info("xG proxy calibré sur %d lignes de xG réel (R²=%.3f)", has_real.sum(),
                 model.score(X[has_real], df.loc[has_real, "xg_for"]))
    else:
        model = PoissonRegressor(alpha=1e-4, max_iter=500).fit(X, df["goals_for"])
        log.info("xG proxy : régression de Poisson sur les buts (%d lignes)", len(df))

    df["xg_proxy"] = np.clip(model.predict(X), 0, None).round(2)
    wide = df.pivot_table(index="match_id", columns="is_home", values="xg_proxy")
    return pd.DataFrame({"match_id": wide.index, "home_xg_proxy": wide.get(1), "away_xg_proxy": wide.get(0)}
                        ).dropna().reset_index(drop=True)


@dataclass
class TeamStrengths:
    attack: pd.Series
    defence: pd.Series
    home_adv: float
    intercept: float

    def expected_goals(self, home: str, away: str) -> tuple[float, float]:
        lam_h = np.exp(self.intercept + self.home_adv + self.attack.get(home, 0) - self.defence.get(away, 0))
        lam_a = np.exp(self.intercept + self.attack.get(away, 0) - self.defence.get(home, 0))
        return float(lam_h), float(lam_a)

    def outcome_probs(self, home: str, away: str, max_goals: int = 10) -> dict[str, float]:
        lam_h, lam_a = self.expected_goals(home, away)
        goals = np.arange(max_goals + 1)
        matrix = np.outer(poisson.pmf(goals, lam_h), poisson.pmf(goals, lam_a))
        return {"p_home": float(np.tril(matrix, -1).sum()), "p_draw": float(np.trace(matrix)),
                "p_away": float(np.triu(matrix, 1).sum()), "xg_home": lam_h, "xg_away": lam_a}


@skill("analysis.team_strengths", "analysis",
       "Forces attaque/défense (Poisson pondéré dans le temps) + probabilités 1X2")
def fit_team_strengths(matches: pd.DataFrame, as_of: pd.Timestamp | None = None,
                       half_life_days: float = 180.0, alpha: float = 1e-3) -> TeamStrengths:
    """`matches` : une ligne par match (home_team, away_team, fthg, ftag, match_date)."""
    df = matches.dropna(subset=["fthg", "ftag"]).copy()
    as_of = pd.Timestamp(as_of) if as_of is not None else df["match_date"].max()
    df = df[df["match_date"] <= as_of]
    age = (as_of - df["match_date"]).dt.days.to_numpy()
    w = 0.5 ** (age / half_life_days)

    teams = sorted(set(df["home_team"]) | set(df["away_team"]))
    idx = {t: i for i, t in enumerate(teams)}
    n, k = len(df), len(teams)
    # Deux observations par match : (attaque domicile vs défense ext.) et l'inverse
    X = np.zeros((2 * n, 2 * k + 1))
    y = np.concatenate([df["fthg"].to_numpy(), df["ftag"].to_numpy()]).astype(float)
    for r, (h, a) in enumerate(zip(df["home_team"], df["away_team"])):
        X[r, idx[h]] = 1;      X[r, k + idx[a]] = -1;      X[r, 2 * k] = 1   # noqa: E702
        X[n + r, idx[a]] = 1;  X[n + r, k + idx[h]] = -1                     # noqa: E702
    model = PoissonRegressor(alpha=alpha, max_iter=1000).fit(X, y, sample_weight=np.concatenate([w, w]))
    coef = model.coef_
    return TeamStrengths(
        attack=pd.Series(coef[:k], index=teams),
        defence=pd.Series(coef[k:2 * k], index=teams),
        home_adv=float(coef[2 * k]),
        intercept=float(model.intercept_),
    )


@skill("analysis.predict_fixtures", "analysis", "Probabilités 1X2 et xG attendus pour des fixtures")
def predict_fixtures(strengths: TeamStrengths, fixtures: pd.DataFrame) -> pd.DataFrame:
    preds = [strengths.outcome_probs(h, a) for h, a in zip(fixtures["home_team"], fixtures["away_team"])]
    return pd.concat([fixtures.reset_index(drop=True), pd.DataFrame(preds)], axis=1)
