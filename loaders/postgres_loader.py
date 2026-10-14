"""Chargement en étoile vers PostgreSQL.

Ordre imposé par les clés étrangères :
    dimensions (équipes, alias, stades, arbitres, gameweeks, joueurs)
      -> fact_matches
      -> liens fixture FPL <-> match (gameweek_id, fpl_fixture_id)
      -> fact_fpl_history / fact_player_match_stats
      -> agrégats dérivés (xG équipe) + vues matérialisées

Toutes les écritures sont des upserts sur clé naturelle => rejouables à volonté.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable

import pandas as pd
from sqlalchemy import text

from config.settings import CONFIG_DIR
from core.db import get_engine, read_sql, upsert_dataframe
from core.seasons import Season
from transformers.fpl import FplBundle
from transformers.normalization import get_team_resolver
from skills.registry import skill

log = logging.getLogger(__name__)


# =============================================================== DIMENSIONS
def ensure_teams(names: Iterable[str]) -> dict[str, int]:
    resolver = get_team_resolver()
    names = sorted({n for n in names if isinstance(n, str) and n})
    if names:
        df = pd.DataFrame({"team_name": names, "short_name": [resolver.short_name(n) for n in names]})
        upsert_dataframe(df, "dim_teams", ["team_name"], update_cols=[], hash_col=None)
    return team_ids()


def team_ids() -> dict[str, int]:
    return dict(read_sql("SELECT team_name, team_id FROM pl.dim_teams").itertuples(index=False, name=None))


@skill("load.reference", "load", "Charge équipes canoniques, alias, stades (référentiels)")
def load_reference_data() -> None:
    mapping = pd.read_csv(CONFIG_DIR / "team_mapping.csv")
    ids = ensure_teams(mapping["canonical_name"])
    aliases = get_team_resolver().aliases_frame()
    aliases["team_id"] = aliases["team_name"].map(ids)
    aliases["source"] = "team_mapping.csv"
    upsert_dataframe(aliases.dropna(subset=["team_id"]), "dim_team_aliases", ["alias"], hash_col=None)

    venues = pd.read_csv(CONFIG_DIR / "venues.csv")
    upsert_dataframe(venues.rename(columns={"venue": "venue_name"})[["venue_name", "city", "capacity"]]
                     .drop_duplicates("venue_name"), "dim_venues", ["venue_name"], hash_col=None)


def _venue_for(team: str, season_id: int, venues: pd.DataFrame) -> str | None:
    rows = venues[(venues["team"] == team) & (venues["from_season"] <= season_id)
                  & (venues["to_season"].isna() | (venues["to_season"] >= season_id))]
    return rows["venue"].iloc[-1] if not rows.empty else None


def upsert_team_seasons(season: Season, teams: Iterable[str], fpl_ids: dict[str, int] | None = None) -> None:
    """Pont équipe × saison (+ stade de la saison, + id FPL saisonnier)."""
    ids = ensure_teams(teams)
    venues = pd.read_csv(CONFIG_DIR / "venues.csv")
    venue_ids = dict(read_sql("SELECT venue_name, venue_id FROM pl.dim_venues").itertuples(index=False, name=None))
    rows = [{
        "team_id": ids[t], "season_id": season.season_id,
        "venue_id": venue_ids.get(_venue_for(t, season.season_id, venues)),
        **({"fpl_team_id": fpl_ids[t]} if fpl_ids and t in fpl_ids else {}),
    } for t in sorted(set(teams))]
    df = pd.DataFrame(rows)
    upsert_dataframe(df, "bridge_team_season", ["team_id", "season_id"], hash_col=None)


def ensure_referees(names: Iterable[str]) -> dict[str, int]:
    names = sorted({n for n in names if isinstance(n, str) and n})
    if names:
        upsert_dataframe(pd.DataFrame({"referee_name": names}), "dim_referees", ["referee_name"],
                         update_cols=[], hash_col=None)
    return dict(read_sql("SELECT referee_name, referee_id FROM pl.dim_referees").itertuples(index=False, name=None))


def _date_id(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s).dt.strftime("%Y%m%d").astype(int)


# ================================================================== MATCHES
@skill("load.matches", "load", "Upsert fact_matches (clé naturelle saison × domicile × extérieur)")
def load_matches(df: pd.DataFrame, season: Season, only_insert: bool = False) -> int:
    if df.empty:
        return 0
    teams = pd.concat([df["home_team"], df["away_team"]]).unique()
    upsert_team_seasons(season, teams)
    ids = team_ids()
    referees = ensure_referees(df["referee"].dropna()) if "referee" in df else {}
    venues = dict(read_sql(
        "SELECT team_id, venue_id FROM pl.bridge_team_season WHERE season_id = :s",
        {"s": season.season_id}).itertuples(index=False, name=None))

    out = df.copy()
    out["home_team_id"] = out["home_team"].map(ids)
    out["away_team_id"] = out["away_team"].map(ids)
    out["date_id"] = _date_id(out["match_date"])
    out["venue_id"] = out["home_team_id"].map(venues)
    if "referee" in out:
        out["referee_id"] = out["referee"].map(referees)

    if only_insert:
        # Fixtures à venir : ne jamais écraser un match déjà chargé depuis football-data
        return upsert_dataframe(out, "fact_matches", ["season_id", "home_team_id", "away_team_id"],
                                update_cols=[], hash_col=None)
    return upsert_dataframe(out, "fact_matches", ["season_id", "home_team_id", "away_team_id"])


def match_lookup(season_id: int) -> pd.DataFrame:
    return read_sql("""SELECT m.match_id, ht.team_name AS home_team, at.team_name AS away_team
                       FROM pl.fact_matches m
                       JOIN pl.dim_teams ht ON ht.team_id = m.home_team_id
                       JOIN pl.dim_teams at ON at.team_id = m.away_team_id
                       WHERE m.season_id = :s""", {"s": season_id})


# ====================================================================== FPL
@skill("load.fpl", "load", "Charge un lot FPL (dims joueurs/GW, liens matchs, faits FPL et stats joueurs)")
def load_fpl_bundle(bundle: FplBundle, since_gw: int | None = None) -> dict[str, int]:
    season, sid = bundle.season, bundle.season.season_id
    counts: dict[str, int] = {}

    # 1) Équipes + ids FPL saisonniers
    fpl_ids = dict(zip(bundle.team_ids["team"], bundle.team_ids["fpl_team_id"]))
    upsert_team_seasons(season, bundle.team_ids["team"], fpl_ids)
    ids = team_ids()

    # 2) Gameweeks
    gws = bundle.gameweeks.dropna(subset=["gw_number"])
    counts["dim_gameweeks"] = upsert_dataframe(gws, "dim_gameweeks", ["season_id", "gw_number"], hash_col=None)
    gw_ids = dict(read_sql("SELECT gw_number, gameweek_id FROM pl.dim_gameweeks WHERE season_id = :s",
                           {"s": sid}).itertuples(index=False, name=None))

    # 3) Joueurs (identité stable = fpl_code) puis attributs saisonniers
    counts["dim_players"] = upsert_dataframe(bundle.players, "dim_players", ["fpl_code"], hash_col=None)
    player_ids = dict(read_sql("SELECT fpl_code, player_id FROM pl.dim_players").itertuples(index=False, name=None))
    ps = bundle.player_season.copy()
    ps["player_id"] = ps["fpl_code"].map(player_ids)
    ps["team_id"] = ps["team"].map(ids)
    ps["position"] = ps["position"].fillna("MID")
    counts["dim_player_season"] = upsert_dataframe(ps.dropna(subset=["player_id"]), "dim_player_season",
                                                   ["player_id", "season_id"], hash_col=None)

    # 4) Lien fixture FPL -> match (gameweek + id fixture sur fact_matches)
    fx = bundle.fixtures.merge(match_lookup(sid), on=["home_team", "away_team"], how="left")
    linked = fx.dropna(subset=["match_id"])
    if not linked.empty:
        payload = [{"match_id": int(r.match_id), "fx": int(r.fpl_fixture_id),
                    "gw": gw_ids.get(int(r.gw_number)) if pd.notna(r.gw_number) else None}
                   for r in linked.itertuples()]
        with get_engine().begin() as conn:
            conn.execute(text("""UPDATE pl.fact_matches SET fpl_fixture_id = :fx,
                                 gameweek_id = COALESCE(:gw, gameweek_id), updated_at = now()
                                 WHERE match_id = :match_id
                                   AND (fpl_fixture_id IS DISTINCT FROM :fx OR gameweek_id IS DISTINCT FROM :gw)"""),
                         payload)
    fixture_to_match = dict(zip(linked["fpl_fixture_id"], linked["match_id"].astype(int)))
    unlinked = fx["match_id"].isna() & fx["finished"]
    if unlinked.any():
        log.warning("%s : %d fixtures FPL terminées sans match correspondant", season, int(unlinked.sum()))

    # 5) Faits FPL
    hist = bundle.history
    if since_gw is not None:
        hist = hist[hist["round"] >= since_gw]
    if hist.empty:
        return counts
    f = hist.copy()
    f["player_id"] = f["fpl_code"].map(player_ids)
    f["gameweek_id"] = f["round"].map(gw_ids)
    f["team_id"] = f["team"].map(ids)
    f["opponent_team_id"] = f["opponent_team"].map(ids)
    f["match_id"] = f["fixture"].map(fixture_to_match)
    f = f.rename(columns={"fixture": "fpl_fixture_id", "selected": "selected_by"})
    f = f.dropna(subset=["player_id", "gameweek_id"])
    counts["fact_fpl_history"] = upsert_dataframe(f, "fact_fpl_history",
                                                  ["player_id", "season_id", "fpl_fixture_id"])

    # match_id ne fait pas partie du record_hash : une ligne inchangée ne serait jamais re-liée
    # si son match arrive après coup (match en quarantaine puis corrigé, report…). Rattrapage SQL :
    with get_engine().begin() as conn:
        conn.execute(text("""
            UPDATE pl.fact_fpl_history h SET match_id = m.match_id
            FROM pl.fact_matches m
            WHERE h.season_id = :s AND m.season_id = :s AND h.match_id IS NULL
              AND m.fpl_fixture_id = h.fpl_fixture_id"""), {"s": sid})

    stats = f.dropna(subset=["match_id", "team_id", "opponent_team_id"])
    stats = stats.drop(columns=["record_hash"])
    counts["fact_player_match_stats"] = upsert_dataframe(stats, "fact_player_match_stats",
                                                         ["player_id", "match_id"], hash_col=None)

    # 6) xG équipe = somme des xG joueurs (disponible depuis 2022-23 dans les données FPL)
    with get_engine().begin() as conn:
        conn.execute(text("""
            UPDATE pl.fact_matches m SET home_xg = x.home_xg, away_xg = x.away_xg, updated_at = now()
            FROM (SELECT s.match_id,
                         SUM(s.expected_goals) FILTER (WHERE s.was_home)     AS home_xg,
                         SUM(s.expected_goals) FILTER (WHERE NOT s.was_home) AS away_xg
                  FROM pl.fact_player_match_stats s
                  WHERE s.season_id = :s AND s.expected_goals IS NOT NULL
                  GROUP BY s.match_id) x
            WHERE m.match_id = x.match_id
              AND (m.home_xg IS DISTINCT FROM x.home_xg OR m.away_xg IS DISTINCT FROM x.away_xg)"""),
            {"s": sid})
    return counts


@skill("load.fpl_snapshot", "load", "Upsert du snapshot quotidien FPL (prix / forme / sélection)")
def load_fpl_snapshot(df: pd.DataFrame) -> int:
    player_ids = dict(read_sql("SELECT fpl_code, player_id FROM pl.dim_players").itertuples(index=False, name=None))
    out = df.copy()
    out["player_id"] = out["fpl_code"].map(player_ids)
    return upsert_dataframe(out.dropna(subset=["player_id"]), "fact_fpl_player_snapshot",
                            ["player_id", "snapshot_date"], hash_col=None)


def update_xg_proxy(predictions: pd.DataFrame) -> int:
    """Écrit le xG "proxy" modélisé (analytics.xg_model) pour les saisons sans xG FPL."""
    if predictions.empty:
        return 0
    with get_engine().begin() as conn:
        res = conn.execute(text("""UPDATE pl.fact_matches SET home_xg_proxy = :h, away_xg_proxy = :a
                                   WHERE match_id = :match_id"""),
                           [{"match_id": int(r.match_id), "h": round(float(r.home_xg_proxy), 2),
                             "a": round(float(r.away_xg_proxy), 2)} for r in predictions.itertuples()])
    return res.rowcount or 0
