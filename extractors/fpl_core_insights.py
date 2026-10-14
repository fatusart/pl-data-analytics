"""Extraction optionnelle du dépôt FPL-Core-Insights (stats avancées des saisons récentes).

Désactivé par défaut (`enabled: false` dans sources.yaml) : la structure du dépôt évolue,
vérifier les chemins avant activation. Un 404 n'interrompt jamais le pipeline.
"""
from __future__ import annotations

import logging
from pathlib import Path

from config.settings import get_settings
from core.http import NotFound, client_for
from core.seasons import Season
from extractors.base import raw_path, should_skip_frozen
from skills.registry import skill

log = logging.getLogger(__name__)
SOURCE = "fpl_core_insights"


@skill("retrieval.fpl_core_insights", "retrieval",
       "Stats avancées FPL-Core-Insights (optionnel, saisons récentes)", tags=("optional",))
def extract_fpl_core_insights(force: bool = False) -> dict[str, dict[str, Path]]:
    src = get_settings().sources[SOURCE]
    if not src.get("enabled"):
        log.info("FPL-Core-Insights désactivé (sources.yaml)")
        return {}
    client = client_for(SOURCE)
    out: dict[str, dict[str, Path]] = {}
    for year in src.get("seasons", []):
        season = Season(year)
        for name, template in src["files"].items():
            rel = template.format(**season.template_vars())
            dest = raw_path(SOURCE, season, f"{name}.csv")
            if should_skip_frozen(dest, season, force):
                out.setdefault(season.label, {})[name] = dest
                continue
            try:
                client.download(rel, dest, force=force)
                out.setdefault(season.label, {})[name] = dest
            except NotFound:
                log.warning("FPL-Core-Insights : %s introuvable — vérifier sources.yaml", rel)
    return out
