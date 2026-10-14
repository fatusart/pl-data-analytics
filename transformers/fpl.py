"""Transformation FPL : archive historique (vaastav) ET API officielle vers un contrat unique.

Points clés de normalisation :
  - Identité joueur stable : le `code` FPL (persistant entre saisons), jamais l'`element` id
    (réattribué chaque saison). Les noms servent uniquement à l'affichage / rapprochement.
  - Équipe du joueur déterminée PAR FIXTURE (team_h/team_a + was_home) => transferts
    en cours de saison correctement attribués (players_raw n'est qu'un instantané de fin de saison).
  - Prix FPL `value` (en dixièmes) converti en £m.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from core.seasons import Season
from extractors.base import read_json
from transformers.normalization import add_record_hash, clean_vaastav_name, get_team_resolver, normalize_key
from transformers.schemas import FplHistoryRecord, validate_frame
from skills.registry import skill

log = logging.getLogger(__name__)

POSITIONS = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD", 5: "MGR"}
STAT_INT_COLS = ["minutes", "starts", "goals_scored", "assists", "clean_sheets", "goals_conceded",
                 "own_goals", "penalties_saved", "penalties_missed", "yellow_cards", "red_cards",
                 "saves", "bonus", "bps", "total_points", "selected", "transfers_in", "transfers_out",
                 "transfers_balance", "defensive_contribution"]
STAT_FLOAT_COLS = ["influence", "creativity", "threat", "ict_index", "expected_goals", "expected_assists",
                   "expected_goal_involvements", "expected_goals_conceded"]
HASH_COLS = ["fpl_code", "fixture", "round", "team", "opponent_team", "minutes", "total_points", "bonus",
             "bps", "price", "selected", "transfers_in", "transfers_out", "expected_goals", "expected_assists"]


@dataclass
class FplBundle:
    season: Season
    history: pd.DataFrame                      # joueur × fixture
    players: pd.DataFrame                      # dim_players
    player_season: pd.DataFrame                # dim_player_season
    gameweeks: pd.DataFrame                    # dim_gameweeks
    fixtures: pd.DataFrame                     # fixture FPL -> affiche (lien fact_matches)
    team_ids: pd.DataFrame                     # fpl_team_id -> équipe canonique (bridge_team_season)
    rejects: list[dict] = field(default_factory=list)


# --------------------------------------------------------------------------- helpers
def _read_csv(path: Path) -> pd.DataFrame:
    for enc in ("utf-8", "latin-1"):
        try:
            return pd.read_csv(path, encoding=enc, low_memory=False)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"Encodage illisible : {path}")


def _to_bool(s: pd.Series) -> pd.Series:
    return s.astype(str).str.strip().str.lower().isin({"true", "1", "t"})


def _fixtures_frame(fx: pd.DataFrame, team_names: dict[int, str], season: Season) -> pd.DataFrame:
    out = pd.DataFrame({
        "season_id": season.season_id,
        "fpl_fixture_id": fx["id"].astype(int),
        "gw_number": pd.to_numeric(fx["event"], errors="coerce").astype("Int64"),
        "kickoff_at": pd.to_datetime(fx["kickoff_time"], utc=True, errors="coerce"),
        "home_team": fx["team_h"].astype(int).map(team_names),
        "away_team": fx["team_a"].astype(int).map(team_names),
        "finished": _to_bool(fx["finished"]),
        "fpl_team_h": fx["team_h"].astype(int),
        "fpl_team_a": fx["team_a"].astype(int),
    })
    return out


def _fixtures_from_gw(hist: pd.DataFrame, team_names: dict[int, str], season: Season) -> pd.DataFrame:
    """Reconstruit les fixtures depuis merged_gw : les joueurs à l'extérieur ont pour adversaire
    l'équipe à domicile, et inversement."""
    h = hist.assign(opp=pd.to_numeric(hist["opponent_team"], errors="coerce"))
    home = h[~h["was_home"]].groupby("fixture")["opp"].agg(lambda s: s.mode().iat[0]).rename("team_h")
    away = h[h["was_home"]].groupby("fixture")["opp"].agg(lambda s: s.mode().iat[0]).rename("team_a")
    meta = h.groupby("fixture").agg(event=("round", "first"), kickoff_time=("kickoff_time", "first"))
    fx = meta.join(home).join(away).dropna(subset=["team_h", "team_a"]).reset_index()
    fx = fx.rename(columns={"fixture": "id"}).assign(finished=True)
    return _fixtures_frame(fx, team_names, season)


def _assign_teams(hist: pd.DataFrame, fixtures: pd.DataFrame, team_names: dict[int, str],
                  fallback_team: pd.Series | None) -> pd.DataFrame:
    """Équipe du joueur = côté de la fixture correspondant à was_home."""
    hist["opponent_team"] = hist["opponent_team"].astype(int).map(team_names)
    if not fixtures.empty:
        fx = fixtures.set_index("fpl_fixture_id")
        hist["team"] = [
            (fx.at[f, "home_team"] if h else fx.at[f, "away_team"]) if f in fx.index else None
            for f, h in zip(hist["fixture"], hist["was_home"])
        ]
    else:
        hist["team"] = None
    if fallback_team is not None:
        hist["team"] = hist["team"].fillna(fallback_team)
    return hist


def _finalize(hist: pd.DataFrame, season: Season) -> tuple[pd.DataFrame, list[dict]]:
    hist["season_id"] = season.season_id
    for col in STAT_INT_COLS:
        hist[col] = pd.to_numeric(hist[col], errors="coerce").astype("Int64") if col in hist else pd.NA
    for col in STAT_FLOAT_COLS:
        hist[col] = pd.to_numeric(hist[col], errors="coerce") if col in hist else float("nan")
    hist["price"] = pd.to_numeric(hist["value"], errors="coerce") / 10.0
    hist["round"] = pd.to_numeric(hist["round"], errors="coerce").astype("Int64")
    hist["kickoff_at"] = pd.to_datetime(hist["kickoff_time"], utc=True, errors="coerce")
    hist = hist.drop_duplicates(["fpl_code", "fixture"], keep="last")
    valid, rejects = validate_frame(hist, FplHistoryRecord)
    for col in ("fpl_code", "element", "fixture"):
        valid[col] = valid[col].astype(int)
    valid = add_record_hash(valid, HASH_COLS)
    return valid.reset_index(drop=True), rejects


def _player_frames(hist: pd.DataFrame, ref: pd.DataFrame, season: Season) -> tuple[pd.DataFrame, pd.DataFrame]:
    players = ref[["code", "first_name", "second_name", "web_name"]].rename(columns={"code": "fpl_code"})
    players = players.drop_duplicates("fpl_code").copy()
    players["name_key"] = (players["first_name"].fillna("") + " " + players["second_name"].fillna("")).map(normalize_key)

    ordered = hist.sort_values(["fpl_code", "round", "kickoff_at"])
    prices = ordered.groupby("fpl_code").agg(start_price=("price", "first"), current_price=("price", "last"),
                                             last_team=("team", "last"))
    ps = ref[["code", "id", "element_type", "team_name"]].rename(
        columns={"code": "fpl_code", "id": "fpl_element_id"})
    ps["position"] = ps["element_type"].map(POSITIONS)
    ps = ps.join(prices, on="fpl_code")
    # Équipe de référence : dernière équipe observée en match, sinon référentiel
    ps["team"] = ps["last_team"].fillna(ps["team_name"])
    ps["season_id"] = season.season_id
    return players, ps[["fpl_code", "season_id", "fpl_element_id", "team", "position",
                        "start_price", "current_price"]]


# --------------------------------------------------------------------- historique
@skill("transform.fpl_history", "transform",
       "Normalise une saison FPL archivée (merged_gw + players_raw) vers le contrat unifié")
def transform_fpl_history_season(season: Season, files: dict[str, Path], master_team_list: Path) -> FplBundle:
    resolver = get_team_resolver()
    if "teams" in files:
        teams = _read_csv(files["teams"])
        team_names = {int(r.id): resolver.resolve(r.name) for r in teams.itertuples()}
    else:
        mtl = _read_csv(master_team_list)
        mtl = mtl[mtl["season"] == season.label]
        team_names = {int(r.team): resolver.resolve(r.team_name) for r in mtl.itertuples()}
    if len(team_names) != 20:
        raise ValueError(f"FPL {season} : {len(team_names)} équipes trouvées au lieu de 20 "
                         "(teams.csv / master_team_list.csv)")

    ref = _read_csv(files["players_raw"])
    ref["team_name"] = ref["team"].astype(int).map(team_names)

    fixtures = (_fixtures_frame(_read_csv(files["fixtures"]), team_names, season)
                if "fixtures" in files else pd.DataFrame())

    hist = _read_csv(files["merged_gw"])
    if "round" not in hist and "GW" in hist:
        hist["round"] = hist["GW"]
    hist["element"] = pd.to_numeric(hist["element"], errors="coerce").astype("Int64")
    hist = hist.dropna(subset=["element"])
    hist["was_home"] = _to_bool(hist["was_home"])

    if fixtures.empty:
        # fixtures.csv absent des premières saisons de l'archive : on reconstruit les affiches
        fixtures = _fixtures_from_gw(hist, team_names, season)

    lookup = ref.set_index("id")
    hist["fpl_code"] = hist["element"].map(lookup["code"])
    hist["web_name"] = hist["element"].map(lookup["web_name"]).fillna(hist["name"].map(clean_vaastav_name))
    hist = _assign_teams(hist, fixtures, team_names, hist["element"].map(lookup["team_name"]))

    hist, rejects = _finalize(hist, season)
    players, player_season = _player_frames(hist, ref, season)

    gws = (hist.groupby("round")["kickoff_at"].agg(first_kickoff="min", last_kickoff="max")
               .reset_index().rename(columns={"round": "gw_number"}))
    gws["season_id"], gws["is_finished"], gws["deadline_at"] = season.season_id, True, pd.NaT

    team_ids = pd.DataFrame({"fpl_team_id": list(team_names), "team": list(team_names.values()),
                             "season_id": season.season_id})
    log.info("FPL %s : %d lignes joueur×fixture, %d joueurs, %d rejets",
             season, len(hist), len(players), len(rejects))
    return FplBundle(season, hist, players, player_season, gws, fixtures, team_ids, rejects)


# ---------------------------------------------------------------- saison en cours
@skill("transform.fpl_current", "transform",
       "Normalise la saison en cours depuis l'API FPL (bootstrap + element-summary + fixtures)")
def transform_fpl_current(season: Season, bootstrap_path: Path, fixtures_path: Path,
                          element_summary_dir: Path) -> FplBundle:
    resolver = get_team_resolver()
    bootstrap = read_json(bootstrap_path)
    team_names = {t["id"]: resolver.resolve(t["name"]) for t in bootstrap["teams"]}

    ref = pd.DataFrame(bootstrap["elements"])
    ref["team_name"] = ref["team"].map(team_names)
    fixtures = _fixtures_frame(pd.DataFrame(read_json(fixtures_path)), team_names, season)

    rows: list[dict] = []
    for path in sorted(Path(element_summary_dir).glob("*.json")):
        rows.extend(read_json(path).get("history", []))
    hist = pd.DataFrame(rows)
    if hist.empty:
        log.warning("Aucun historique joueur pour %s (début de saison ?)", season)
        hist = pd.DataFrame(columns=["element", "fixture", "opponent_team", "was_home", "round",
                                     "value", "kickoff_time"])
    hist["was_home"] = _to_bool(hist["was_home"])
    lookup = ref.set_index("id")
    hist["fpl_code"] = hist["element"].map(lookup["code"])
    hist["web_name"] = hist["element"].map(lookup["web_name"])
    hist = _assign_teams(hist, fixtures, team_names, hist["element"].map(lookup["team_name"]))
    hist, rejects = _finalize(hist, season)
    players, player_season = _player_frames(hist, ref, season)
    # Pour la saison en cours, le prix "courant" est celui du jour (now_cost), pas celui du dernier match
    player_season["current_price"] = player_season["fpl_code"].map(
        ref.set_index("code")["now_cost"] / 10.0)

    events = pd.DataFrame(bootstrap["events"])
    gws = pd.DataFrame({
        "season_id": season.season_id,
        "gw_number": events["id"],
        "deadline_at": pd.to_datetime(events["deadline_time"], utc=True),
        "is_finished": events["finished"].astype(bool),
        "avg_points": events.get("average_entry_score"),
        "highest_points": events.get("highest_score"),
    })
    ko = fixtures.groupby("gw_number")["kickoff_at"].agg(first_kickoff="min", last_kickoff="max")
    gws = gws.join(ko, on="gw_number")

    team_ids = pd.DataFrame({"fpl_team_id": list(team_names), "team": list(team_names.values()),
                             "season_id": season.season_id})
    return FplBundle(season, hist, players, player_season, gws, fixtures, team_ids, rejects)


@skill("transform.fpl_snapshot", "transform", "Instantané quotidien prix / forme / sélection (bootstrap)")
def transform_fpl_snapshot(bootstrap_path: Path, season: Season, snapshot_date) -> pd.DataFrame:
    ref = pd.DataFrame(read_json(bootstrap_path)["elements"])
    return pd.DataFrame({
        "fpl_code": ref["code"],
        "snapshot_date": snapshot_date,
        "season_id": season.season_id,
        "price": ref["now_cost"] / 10.0,
        "selected_by_pct": pd.to_numeric(ref["selected_by_percent"], errors="coerce"),
        "form": pd.to_numeric(ref["form"], errors="coerce"),
        "points_per_game": pd.to_numeric(ref["points_per_game"], errors="coerce"),
        "total_points": ref["total_points"],
        "transfers_in_event": ref["transfers_in_event"],
        "transfers_out_event": ref["transfers_out_event"],
        "status": ref["status"],
        "chance_of_playing_next_round": ref["chance_of_playing_next_round"],
        "news": ref["news"].replace("", None),
    })


def upcoming_matches(bundle: FplBundle) -> pd.DataFrame:
    """Fixtures non jouées de la saison courante -> lignes fact_matches (is_played = FALSE).
    Indispensables pour le calendrier et la difficulté des prochains matchs (FDR)."""
    fx = bundle.fixtures[~bundle.fixtures["finished"] & bundle.fixtures["kickoff_at"].notna()].copy()
    if fx.empty:
        return pd.DataFrame()
    fx["match_date"] = fx["kickoff_at"].dt.tz_convert("Europe/London").dt.date
    fx["is_played"], fx["source"] = False, "fpl"
    fx = add_record_hash(fx, ["season_id", "fpl_fixture_id", "kickoff_at", "home_team", "away_team"])
    return fx
