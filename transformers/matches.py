"""Transformation des CSV football-data.co.uk vers le contrat `fact_matches`.

Gère l'hétérogénéité inter-saisons :
  - dates 'dd/mm/yy' (anciennes saisons) vs 'dd/mm/yyyy' ; colonne `Time` absente avant 2019-20 ;
  - cotes agrégées 'BbAvH/BbMxH' (≤ 2018-19) renommées 'AvgH/MaxH' ensuite ;
  - libellés d'équipes propres à la source => nom canonique.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from config.settings import get_settings
from core.seasons import Season
from transformers.normalization import add_record_hash, clean_referee, get_team_resolver
from transformers.schemas import MatchRecord, validate_frame
from skills.registry import skill

log = logging.getLogger(__name__)

# colonne cible -> candidats source, par ordre de préférence
COLUMN_MAP: dict[str, list[str]] = {
    "fthg": ["FTHG", "HG"], "ftag": ["FTAG", "AG"], "ftr": ["FTR", "Res"],
    "hthg": ["HTHG"], "htag": ["HTAG"], "htr": ["HTR"],
    "referee": ["Referee"],
    "home_shots": ["HS"], "away_shots": ["AS"],
    "home_sot": ["HST"], "away_sot": ["AST"],
    "home_fouls": ["HF"], "away_fouls": ["AF"],
    "home_corners": ["HC"], "away_corners": ["AC"],
    "home_yellow": ["HY"], "away_yellow": ["AY"],
    "home_red": ["HR"], "away_red": ["AR"],
    "odds_b365_h": ["B365H"], "odds_b365_d": ["B365D"], "odds_b365_a": ["B365A"],
    "odds_ps_h": ["PSH"], "odds_ps_d": ["PSD"], "odds_ps_a": ["PSA"],
    "odds_avg_h": ["AvgH", "BbAvH"], "odds_avg_d": ["AvgD", "BbAvD"], "odds_avg_a": ["AvgA", "BbAvA"],
    "odds_max_h": ["MaxH", "BbMxH"], "odds_max_d": ["MaxD", "BbMxD"], "odds_max_a": ["MaxA", "BbMxA"],
    "odds_avg_over25": ["Avg>2.5", "BbAv>2.5"], "odds_avg_under25": ["Avg<2.5", "BbAv<2.5"],
}
INT_COLS = ["fthg", "ftag", "hthg", "htag", "home_shots", "away_shots", "home_sot", "away_sot",
            "home_fouls", "away_fouls", "home_corners", "away_corners",
            "home_yellow", "away_yellow", "home_red", "away_red"]
HASH_COLS = ["season_id", "match_date", "home_team", "away_team", "referee", *INT_COLS,
             "ftr", "htr", "odds_b365_h", "odds_avg_h", "odds_ps_h"]


def read_football_data_csv(path: Path) -> pd.DataFrame:
    encodings = get_settings().sources["football_data"]["encoding_candidates"]
    last_exc: Exception | None = None
    for enc in encodings:
        try:
            return pd.read_csv(path, encoding=enc, on_bad_lines="warn")
        except UnicodeDecodeError as exc:
            last_exc = exc
    raise ValueError(f"Impossible de décoder {path}") from last_exc


def _first_available(raw: pd.DataFrame, candidates: list[str]) -> pd.Series:
    """Coalesce des colonnes candidates (une saison peut avoir les deux, partiellement remplies)."""
    out: pd.Series | None = None
    for col in candidates:
        if col in raw.columns:
            out = raw[col] if out is None else out.combine_first(raw[col])
    return out if out is not None else pd.Series(pd.NA, index=raw.index, dtype="object")


@skill("transform.matches", "transform",
       "Nettoie/normalise un CSV football-data vers le contrat fact_matches (+ quarantaine)")
def transform_football_data(path: Path, season: Season) -> tuple[pd.DataFrame, list[dict]]:
    raw = read_football_data_csv(path)
    raw = raw.dropna(subset=["HomeTeam", "AwayTeam"]).copy()
    resolver = get_team_resolver()

    df = pd.DataFrame(index=raw.index)
    df["season_id"] = season.season_id
    df["home_team"] = resolver.resolve_series(raw["HomeTeam"].str.strip())
    df["away_team"] = resolver.resolve_series(raw["AwayTeam"].str.strip())

    dates = pd.to_datetime(raw["Date"], dayfirst=True, format="mixed", errors="coerce")
    df["match_date"] = dates.dt.date
    if "Time" in raw.columns:
        local = pd.to_datetime(dates.dt.strftime("%Y-%m-%d") + " " + raw["Time"].fillna("").astype(str),
                               errors="coerce")
        df["kickoff_at"] = (local.dt.tz_localize("Europe/London", ambiguous="NaT", nonexistent="NaT")
                                 .dt.tz_convert("UTC"))
    else:
        df["kickoff_at"] = pd.NaT

    for target, candidates in COLUMN_MAP.items():
        df[target] = _first_available(raw, candidates)
    for col in INT_COLS:
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")
    for col in [c for c in COLUMN_MAP if c.startswith("odds_")]:
        df[col] = pd.to_numeric(df[col], errors="coerce").round(2)
    df["referee"] = df["referee"].map(clean_referee)
    df["is_played"] = df["ftr"].notna()
    df["source"] = "football-data"

    # Doublons : même affiche dans la même saison => on garde la dernière occurrence
    before = len(df)
    df = df.dropna(subset=["match_date"]).drop_duplicates(["season_id", "home_team", "away_team"], keep="last")
    if len(df) != before:
        log.warning("%s : %d doublons/lignes sans date supprimés", season, before - len(df))

    # Erreur de saisie connue de la source (ex. 2021-22 NEW-WHU : 9 tirs cadrés pour 8 tirs) :
    # on garde le résultat du match et on neutralise seulement la paire de stats incohérente.
    for side in ("home", "away"):
        bad = df[f"{side}_sot"] > df[f"{side}_shots"]
        if bad.any():
            log.warning("%s : %d matchs avec tirs cadrés > tirs (%s) — stats de tirs mises à NULL",
                        season, int(bad.sum()), side)
            df.loc[bad.fillna(False), [f"{side}_shots", f"{side}_sot"]] = pd.NA

    valid, rejects = validate_frame(df, MatchRecord)
    valid = add_record_hash(valid, HASH_COLS)
    log.info("football-data %s : %d matchs valides, %d rejetés", season, len(valid), len(rejects))
    return valid.reset_index(drop=True), rejects
