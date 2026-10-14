"""Conventions communes aux extracteurs : zone raw immuable organisée par source/saison."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from config.settings import get_settings
from core.seasons import Season

log = logging.getLogger(__name__)


def raw_path(source: str, season: Season | None, filename: str) -> Path:
    base = get_settings().raw_dir / source
    if season is not None:
        base = base / season.label
    return base / filename


def should_skip_frozen(path: Path, season: Season, force: bool) -> bool:
    """Saison figée déjà téléchargée => aucune requête réseau (batch historique idempotent)."""
    if season.is_frozen and path.exists() and path.stat().st_size > 0 and not force:
        log.debug("Saison figée %s déjà présente : %s", season, path.name)
        return True
    return False


def write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return path


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))
