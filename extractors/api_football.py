"""Extraction optionnelle API-Football v3 (api-sports.io).

Utile pour : horaires exacts, arbitres de la saison en cours avant publication du CSV
football-data, effectifs. Quota gratuit très limité => garde-fou de quota journalier.
"""
from __future__ import annotations

import logging
from pathlib import Path

from config.settings import get_settings
from core.http import client_for
from core.seasons import Season, current_season
from extractors.base import raw_path, write_json
from skills.registry import skill

log = logging.getLogger(__name__)
SOURCE = "api_football"


class QuotaExceeded(RuntimeError):
    pass


@skill("retrieval.api_football", "retrieval",
       "API-Football : fixtures + joueurs paginés (optionnel, clé requise)", tags=("optional", "paginated"))
def extract_api_football(season: Season | None = None, include_players: bool = False) -> dict[str, Path]:
    cfg = get_settings()
    if not cfg.api_football_key:
        log.info("API-Football ignorée : PL_API_FOOTBALL_KEY non définie")
        return {}
    src = cfg.sources[SOURCE]
    season = season or current_season()
    client = client_for(SOURCE, headers={"x-apisports-key": cfg.api_football_key})
    params = {"league": src["league_id"], "season": season.start_year}

    out: dict[str, Path] = {}
    fixtures = client.get_json("/fixtures", params=params)
    _check_quota(fixtures, src["daily_quota"])
    out["fixtures"] = write_json(raw_path(SOURCE, season, "fixtures.json"), fixtures.get("response", []))

    if include_players:
        players = list(client.paginate("/players", params=params))
        out["players"] = write_json(raw_path(SOURCE, season, "players.json"), players)
    return out


def _check_quota(payload: dict, daily_quota: int) -> None:
    errors = payload.get("errors")
    if errors:
        raise QuotaExceeded(f"API-Football a renvoyé une erreur : {errors}")
    if payload.get("results", 0) == 0:
        log.warning("API-Football : réponse vide (quota ou saison non couverte ?)")
