"""Extraction football-data.co.uk : 1 CSV par saison (E0 = Premier League)."""
from __future__ import annotations

import logging
from pathlib import Path

from config.settings import get_settings
from core.http import NotFound, client_for
from core.seasons import Season, all_seasons
from extractors.base import raw_path, should_skip_frozen
from skills.registry import skill

log = logging.getLogger(__name__)
SOURCE = "football_data"


@skill("retrieval.football_data", "retrieval",
       "Télécharge les CSV de matchs (scores, stats, arbitres, cotes) par saison",
       tags=("batch", "incremental"))
def extract_football_data(seasons: list[Season] | None = None, force: bool = False) -> dict[str, Path]:
    cfg = get_settings().sources[SOURCE]
    client = client_for(SOURCE)
    out: dict[str, Path] = {}

    for season in seasons or all_seasons():
        dest = raw_path(SOURCE, season, "E0.csv")
        if should_skip_frozen(dest, season, force):
            out[season.label] = dest
            continue
        url = cfg["url_template"].format(**season.template_vars())
        try:
            path, changed = client.download(url, dest, force=force)
        except NotFound:
            # Début de saison : le fichier peut ne pas encore exister
            log.warning("football-data : pas de fichier pour %s (%s)", season, url)
            continue
        log.info("football-data %s : %s", season, "mis à jour" if changed else "inchangé")
        out[season.label] = path
    return out
