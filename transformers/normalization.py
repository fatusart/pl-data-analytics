"""Normalisation des identités (équipes, joueurs, arbitres) d'une source et d'une saison à l'autre."""
from __future__ import annotations

import hashlib
import json
import logging
import re
from functools import lru_cache

import pandas as pd
from unidecode import unidecode

from config.settings import CONFIG_DIR

log = logging.getLogger(__name__)

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_key(value: str | None) -> str:
    """'Nott'm Forest' -> 'nottm forest' ; 'Søren Kragh' -> 'soren kragh'."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    s = unidecode(str(value)).lower().replace("'", "").replace("&", " and ")
    return _NON_ALNUM.sub(" ", s).strip()


def clean_vaastav_name(raw: str) -> str:
    """Les premières saisons de l'archive suffixent l'id : 'Aaron_Cresswell_402' -> 'Aaron Cresswell'."""
    s = re.sub(r"_\d+$", "", str(raw))
    return s.replace("_", " ").strip()


def clean_referee(name: str | None) -> str | None:
    """'M Oliver', 'M. Oliver', 'Michael Oliver ' -> 'M Oliver' (format football-data)."""
    if name is None or pd.isna(name) or not str(name).strip():
        return None
    s = re.sub(r"\s+", " ", str(name).replace(".", " ")).strip()
    parts = s.split(" ")
    if len(parts) >= 2 and len(parts[0]) > 1:
        s = f"{parts[0][0]} {' '.join(parts[1:])}"
    return s.title()


class TeamResolver:
    """Résout n'importe quel libellé d'équipe vers son nom canonique via config/team_mapping.csv.

    Un libellé inconnu (ex. promu non encore référencé) n'interrompt pas le pipeline :
    il est créé à la volée, journalisé, et à ajouter au fichier de correspondance.
    """

    def __init__(self, mapping: pd.DataFrame):
        self.alias_to_canonical: dict[str, str] = {}
        self.short_names: dict[str, str] = {}
        for row in mapping.itertuples(index=False):
            self.short_names[row.canonical_name] = row.short_name
            for alias in [row.canonical_name, *str(row.aliases).split("|")]:
                self.alias_to_canonical[normalize_key(alias)] = row.canonical_name
        self.unknown: set[str] = set()

    def resolve(self, name: str) -> str:
        key = normalize_key(name)
        if key in self.alias_to_canonical:
            return self.alias_to_canonical[key]
        canonical = str(name).strip()
        if canonical not in self.unknown:
            log.warning("Équipe non référencée : %r -> ajoutez-la à config/team_mapping.csv", name)
            self.unknown.add(canonical)
        self.alias_to_canonical[key] = canonical
        self.short_names.setdefault(canonical, self._auto_short(canonical))
        return canonical

    def resolve_series(self, s: pd.Series) -> pd.Series:
        uniques = {v: self.resolve(v) for v in s.dropna().unique()}
        return s.map(uniques)

    def short_name(self, canonical: str) -> str:
        return self.short_names.get(canonical) or self._auto_short(canonical)

    def _auto_short(self, canonical: str) -> str:
        base = re.sub(r"[^A-Z]", "", unidecode(canonical).upper())
        candidate = (base + "XXX")[:3]
        taken = set(self.short_names.values())
        i = 0
        while candidate in taken:
            i += 1
            candidate = f"{base[:2]}{i}"[:3]
        return candidate

    def aliases_frame(self) -> pd.DataFrame:
        return pd.DataFrame(
            [{"alias": a, "team_name": c} for a, c in self.alias_to_canonical.items() if a]
        )


@lru_cache
def get_team_resolver() -> TeamResolver:
    return TeamResolver(pd.read_csv(CONFIG_DIR / "team_mapping.csv"))


def add_record_hash(df: pd.DataFrame, cols: list[str], target: str = "record_hash") -> pd.DataFrame:
    """Empreinte md5 du contenu métier : permet un upsert "seulement si changé"."""
    sub = df[cols].astype(object).where(pd.notna(df[cols]), None)
    df[target] = [
        hashlib.md5(json.dumps(row, default=str, separators=(",", ":")).encode()).hexdigest()
        for row in sub.itertuples(index=False, name=None)
    ]
    return df
