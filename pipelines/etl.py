"""Orchestration ETL : backfill historique (saisons figées) et mise à jour incrémentale (saison en cours).

Idempotence à trois niveaux :
  1. Extraction : saisons figées jamais re-téléchargées ; requêtes conditionnelles (ETag) sinon.
  2. Pipeline   : checksum du fichier source mémorisé en watermark => transformation sautée si inchangé.
  3. Base       : upserts sur clé naturelle + `record_hash` => aucune écriture si contenu identique.
"""
from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

from core import db
from core.seasons import Season, current_season, season_range
from extractors.football_data import extract_football_data
from extractors.fpl_api import extract_fpl_current
from extractors.fpl_core_insights import extract_fpl_core_insights
from extractors.fpl_history import extract_fpl_history
from loaders import postgres_loader as loader
from transformers.fpl import (transform_fpl_current, transform_fpl_history_season,
                              transform_fpl_snapshot, upcoming_matches)
from transformers.matches import transform_football_data

log = logging.getLogger(__name__)


def _checksum(path: Path) -> str | None:
    meta = path.with_suffix(path.suffix + ".meta.json")
    return json.loads(meta.read_text()).get("md5") if meta.exists() else None


def _unchanged(source: str, season: Season, checksum: str | None, force: bool) -> bool:
    if force or checksum is None:
        return False
    wm = db.get_watermark(source, season.season_id)
    return bool(wm and wm.get("content_checksum") == checksum)


# ------------------------------------------------------------------ matches
def run_matches(seasons: list[Season], mode: str, force: bool = False) -> None:
    files = extract_football_data(seasons, force=force)
    for season in seasons:
        path = files.get(season.label)
        if path is None:
            continue
        checksum = _checksum(path)
        if _unchanged("football_data", season, checksum, force):
            log.info("football-data %s inchangé depuis le dernier chargement — ignoré", season)
            continue
        with db.etl_run("football_data", mode, season.season_id) as run:
            df, rejects = transform_football_data(path, season)
            run["rows_in"], run["rows_rejected"] = len(df) + len(rejects), len(rejects)
            db.save_rejects(run["run_id"], "football_data", season.season_id, rejects)
            run["rows_upserted"] = loader.load_matches(df, season)
            db.set_watermark("football_data", season.season_id,
                             last_match_date=df["match_date"].max() if not df.empty else None,
                             checksum=checksum)


# --------------------------------------------------------------- FPL (passé)
def run_fpl_history(seasons: list[Season], force: bool = False) -> None:
    frozen = [s for s in seasons if s.is_frozen]
    if not frozen:
        return
    files = extract_fpl_history(frozen, force=force)
    master = files["_global"]["master_team_list"]
    for season in frozen:
        sfiles = files.get(season.label, {})
        if not {"merged_gw", "players_raw"} <= set(sfiles):
            log.error("FPL %s : fichiers manquants (%s) — saison ignorée", season, sorted(sfiles))
            continue
        checksum = _checksum(sfiles["merged_gw"])
        if _unchanged("fpl_history", season, checksum, force):
            log.info("FPL %s déjà chargée — ignorée", season)
            continue
        with db.etl_run("fpl_history", "backfill", season.season_id) as run:
            bundle = transform_fpl_history_season(season, sfiles, master)
            run["rows_in"] = len(bundle.history) + len(bundle.rejects)
            run["rows_rejected"] = len(bundle.rejects)
            db.save_rejects(run["run_id"], "fpl_history", season.season_id, bundle.rejects)
            counts = loader.load_fpl_bundle(bundle)
            run["rows_upserted"] = counts.get("fact_fpl_history", 0)
            db.set_watermark("fpl_history", season.season_id,
                             last_gameweek=int(bundle.history["round"].max()), checksum=checksum)


# ------------------------------------------------------------ FPL (courant)
def run_fpl_current(force: bool = False) -> None:
    season = current_season()
    wm = db.get_watermark("fpl_api", season.season_id) or {}
    last_gw = None if force else wm.get("last_gameweek")
    with db.etl_run("fpl_api", "incremental", season.season_id) as run:
        raw = extract_fpl_current(last_loaded_gw=last_gw, force=force)
        bundle = transform_fpl_current(season, raw["bootstrap"], raw["fixtures"], raw["element_summary_dir"])
        run["rows_in"], run["rows_rejected"] = len(bundle.history), len(bundle.rejects)
        db.save_rejects(run["run_id"], "fpl_api", season.season_id, bundle.rejects)

        loader.load_matches(upcoming_matches(bundle), season, only_insert=True)
        # On recharge la dernière GW connue (corrections de bonus/points a posteriori)
        since = max(1, last_gw - 1) if last_gw else None
        counts = loader.load_fpl_bundle(bundle, since_gw=since)
        run["rows_upserted"] = counts.get("fact_fpl_history", 0)

        snap = transform_fpl_snapshot(raw["bootstrap"], season, date.today())
        loader.load_fpl_snapshot(snap)
        db.set_watermark("fpl_api", season.season_id, last_gameweek=int(raw["finished_gw"] or 0))


# ------------------------------------------------------------- post-traitement
def run_derived() -> None:
    """xG proxy (toutes saisons) puis rafraîchissement des vues matérialisées."""
    from analytics.data_access import team_matches
    from analytics.xg_model import fit_xg_proxy

    db.refresh_materialized_views()
    tm = team_matches()
    if not tm.empty:
        loader.update_xg_proxy(fit_xg_proxy(tm))
    db.refresh_materialized_views()


# ------------------------------------------------------------------ entrées
def backfill(start: str | None = None, end: str | None = None, force: bool = False) -> None:
    seasons = season_range(start, end)
    log.info("Backfill %s -> %s (%d saisons)", seasons[0], seasons[-1], len(seasons))
    loader.load_reference_data()
    run_matches(seasons, "backfill", force)         # les matchs d'abord : les faits FPL s'y rattachent
    run_fpl_history(seasons, force)
    if any(s.is_current for s in seasons):
        run_fpl_current(force)
    extract_fpl_core_insights(force=force)
    run_derived()


def incremental(force: bool = False) -> None:
    season = current_season()
    log.info("Mise à jour incrémentale de la saison %s", season)
    loader.load_reference_data()
    run_matches([season], "incremental", force)
    run_fpl_current(force)
    run_derived()
