"""Registre de "skills" : chaque capacité spécialisée (ingestion, analyse, design) est
déclarée par un décorateur, découvrable (`skills list`) et exécutable (`skills run`).

Cela découple l'orchestration (CLI, scheduler, notebook, dashboard) des implémentations :
on charge une compétence par son nom, sans connaître son module.
"""
from __future__ import annotations

import importlib
import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

Category = Literal["retrieval", "transform", "load", "analysis", "design"]

# Modules importés au démarrage pour peupler le registre
SKILL_MODULES = (
    "extractors.football_data",
    "extractors.fpl_api",
    "extractors.fpl_history",
    "extractors.fpl_core_insights",
    "extractors.api_football",
    "transformers.matches",
    "transformers.fpl",
    "loaders.postgres_loader",
    "analytics.standings",
    "analytics.form",
    "analytics.xg_model",
    "analytics.fpl_metrics",
    "analytics.betting",
    "dashboard.design_tokens",
)


@dataclass(frozen=True)
class Skill:
    name: str
    category: Category
    summary: str
    func: Callable = field(repr=False)
    tags: tuple[str, ...] = ()

    @property
    def signature(self) -> str:
        return str(inspect.signature(self.func))

    def __call__(self, *args, **kwargs):
        return self.func(*args, **kwargs)


_REGISTRY: dict[str, Skill] = {}


def skill(name: str, category: Category, summary: str = "", tags: tuple[str, ...] = ()):
    def decorator(func: Callable) -> Callable:
        if name in _REGISTRY and _REGISTRY[name].func is not func:
            raise ValueError(f"Skill déjà enregistrée : {name}")
        doc = summary or (inspect.getdoc(func) or "").split("\n")[0]
        _REGISTRY[name] = Skill(name, category, doc, func, tags)
        return func
    return decorator


def load_all() -> dict[str, Skill]:
    for mod in SKILL_MODULES:
        importlib.import_module(mod)
    return _REGISTRY


def get_skill(name: str) -> Skill:
    load_all()
    try:
        return _REGISTRY[name]
    except KeyError as exc:
        raise KeyError(f"Skill inconnue : {name}. Disponibles : {sorted(_REGISTRY)}") from exc


def list_skills(category: Category | None = None) -> list[Skill]:
    load_all()
    return sorted((s for s in _REGISTRY.values() if category in (None, s.category)),
                  key=lambda s: (s.category, s.name))
