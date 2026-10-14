"""Configuration centralisée (variables d'environnement préfixées PL_, fichier .env)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import quote_plus

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

CONFIG_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CONFIG_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="PL_", extra="ignore")

    # --- Base de données ---
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "premier_league"
    db_user: str = "pl_user"
    db_password: str = "pl_password"
    db_sslmode: str = "prefer"   # "require" pour une base hébergée (Neon, Supabase…)

    # --- Périmètre temporel ---
    first_season: int = Field(2016, description="Année de début de la 1re saison (2016 => 2016-17)")
    current_season: int = Field(2026, description="Année de début de la saison en cours")

    # --- Stockage local (zone "raw" immuable + zone "staging") ---
    data_dir: Path = PROJECT_ROOT / "data"

    # --- Réseau ---
    http_timeout: float = 30.0
    http_max_retries: int = 5
    http_backoff_base: float = 1.5
    http_backoff_max: float = 60.0
    user_agent: str = "pl-data-platform/1.0 (+educational analytics project)"

    # --- Clés optionnelles ---
    api_football_key: str | None = None

    log_level: str = "INFO"

    @property
    def sqlalchemy_url(self) -> str:
        return (
            f"postgresql+psycopg://{quote_plus(self.db_user)}:{quote_plus(self.db_password)}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}?sslmode={self.db_sslmode}"
        )

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def staging_dir(self) -> Path:
        return self.data_dir / "staging"

    @property
    def sources(self) -> dict:
        return load_sources()


@lru_cache
def load_sources() -> dict:
    with open(CONFIG_DIR / "sources.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@lru_cache
def get_settings() -> Settings:
    return Settings()
