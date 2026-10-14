"""Représentation unique d'une saison et de ses différents codages selon les sources."""
from __future__ import annotations

from dataclasses import dataclass

from config.settings import get_settings


@dataclass(frozen=True, order=True)
class Season:
    start_year: int

    @property
    def season_id(self) -> int:
        return self.start_year

    @property
    def label(self) -> str:
        """'2016-17' (format vaastav / affichage)."""
        return f"{self.start_year}-{(self.start_year + 1) % 100:02d}"

    @property
    def label_long(self) -> str:
        """'2016-2017' (format de certains dépôts open-source)."""
        return f"{self.start_year}-{self.start_year + 1}"

    @property
    def fd_code(self) -> str:
        """'1617' (football-data.co.uk)."""
        return f"{self.start_year % 100:02d}{(self.start_year + 1) % 100:02d}"

    @property
    def is_current(self) -> bool:
        return self.start_year == get_settings().current_season

    @property
    def is_frozen(self) -> bool:
        """Saison terminée : ses données ne changent plus => chargée une seule fois."""
        return self.start_year < get_settings().current_season

    def template_vars(self) -> dict[str, str | int]:
        return {
            "year": self.start_year,
            "label": self.label,
            "label_long": self.label_long,
            "fd_code": self.fd_code,
        }

    @classmethod
    def parse(cls, value: str | int) -> "Season":
        """Accepte 2016, '2016', '2016-17', '2016-2017', '1617'.

        Un nombre à 4 chiffres ≥ 1992 (création de la PL) est une année ; en dessous, c'est un
        code football-data ('1617', '1920'). Les codes ≥ '2021' étant ambigus, préférer '2020-21'.
        """
        s = str(value).strip()
        if len(s) == 4 and s.isdigit():
            if int(s) >= 1992:
                return cls(int(s))
            if int(s[2:]) == (int(s[:2]) + 1) % 100:
                return cls(2000 + int(s[:2]))
        if "-" in s:
            return cls(int(s.split("-")[0]))
        raise ValueError(f"Format de saison non reconnu : {value!r}")

    def __str__(self) -> str:
        return self.label


def all_seasons() -> list[Season]:
    cfg = get_settings()
    return [Season(y) for y in range(cfg.first_season, cfg.current_season + 1)]


def current_season() -> Season:
    return Season(get_settings().current_season)


def season_range(start: str | int | None = None, end: str | int | None = None) -> list[Season]:
    seasons = all_seasons()
    lo = Season.parse(start) if start else seasons[0]
    hi = Season.parse(end) if end else seasons[-1]
    return [s for s in seasons if lo <= s <= hi]
