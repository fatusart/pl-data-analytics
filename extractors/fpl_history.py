"""Extraction des saisons FPL passées depuis l'archive open-source vaastav/Fantasy-Premier-League.

L'API officielle n'expose que la saison en cours : l'historique 2016-17 -> 2025-26 vient
de cette archive (merged_gw.csv = 1 ligne par joueur × fixture, players_raw.csv = référentiel).
"""
from __future__ import annotations

import logging
from pathlib import Path

from config.settings import get_settings
from core.http import NotFound, client_for
from core.seasons import Season, all_seasons
from extractors.base import raw_path, should_skip_frozen
from skills.registry import skill

log = logging.getLogger(__name__)
SOURCE = "fpl_history"


@skill("retrieval.fpl_history", "retrieval",
       "Archive FPL des saisons closes (merged_gw, players_raw, fixtures, master_team_list)",
       tags=("batch",))
def extract_fpl_history(seasons: list[Season] | None = None, force: bool = False) -> dict[str, dict[str, Path]]:
    src = get_settings().sources[SOURCE]
    client = client_for(SOURCE)
    out: dict[str, dict[str, Path]] = {}

    for name, rel in src.get("global_files", {}).items():
        dest = raw_path(SOURCE, None, Path(rel).name)
        if force or not dest.exists():
            client.download(rel, dest, force=force)
        out.setdefault("_global", {})[name] = dest

    # Par défaut : seules les saisons figées (la saison courante vient de l'API officielle)
    targets = seasons or [s for s in all_seasons() if s.is_frozen]
    for season in targets:
        files: dict[str, Path] = {}
        for name, template in src["files"].items():
            rel = template.format(**season.template_vars())
            dest = raw_path(SOURCE, season, Path(rel).name)
            if should_skip_frozen(dest, season, force):
                files[name] = dest
                continue
            try:
                client.download(rel, dest, force=force)
                files[name] = dest
            except NotFound:
                log.warning("fpl_history %s : fichier absent %s (normal pour certaines saisons)", season, rel)
        out[season.label] = files
        log.info("fpl_history %s : %s", season, sorted(files))
    return out
