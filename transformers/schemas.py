"""Contrats de données Pydantic : chaque ligne est validée avant chargement.
Les lignes invalides sont isolées (quarantaine) au lieu de faire échouer le lot."""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

log = logging.getLogger(__name__)

Goals = Field(default=None, ge=0, le=15)
Count = Field(default=None, ge=0, le=60)
Odds = Field(default=None, gt=1.0, le=1000)


class MatchRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    season_id: int = Field(ge=1992, le=2100)
    match_date: date
    kickoff_at: datetime | None = None
    home_team: str = Field(min_length=2)
    away_team: str = Field(min_length=2)
    referee: str | None = None
    fthg: int | None = Goals
    ftag: int | None = Goals
    ftr: Literal["H", "D", "A"] | None = None
    hthg: int | None = Goals
    htag: int | None = Goals
    htr: Literal["H", "D", "A"] | None = None
    home_shots: int | None = Count
    away_shots: int | None = Count
    home_sot: int | None = Count
    away_sot: int | None = Count
    home_fouls: int | None = Count
    away_fouls: int | None = Count
    home_corners: int | None = Count
    away_corners: int | None = Count
    home_yellow: int | None = Field(default=None, ge=0, le=11)
    away_yellow: int | None = Field(default=None, ge=0, le=11)
    home_red: int | None = Field(default=None, ge=0, le=5)
    away_red: int | None = Field(default=None, ge=0, le=5)
    odds_b365_h: float | None = Odds
    odds_b365_d: float | None = Odds
    odds_b365_a: float | None = Odds
    odds_avg_h: float | None = Odds
    odds_avg_d: float | None = Odds
    odds_avg_a: float | None = Odds

    @model_validator(mode="after")
    def check_consistency(self) -> "MatchRecord":
        if self.home_team == self.away_team:
            raise ValueError("équipe domicile = équipe extérieur")
        if None not in (self.fthg, self.ftag, self.ftr):
            expected = "H" if self.fthg > self.ftag else "A" if self.fthg < self.ftag else "D"
            if expected != self.ftr:
                raise ValueError(f"FTR incohérent ({self.fthg}-{self.ftag} vs {self.ftr})")
        if None not in (self.home_shots, self.home_sot) and self.home_sot > self.home_shots:
            raise ValueError("tirs cadrés > tirs (domicile)")
        if None not in (self.away_shots, self.away_sot) and self.away_sot > self.away_shots:
            raise ValueError("tirs cadrés > tirs (extérieur)")
        return self


class FplHistoryRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    season_id: int
    fpl_code: int = Field(gt=0)
    element: int = Field(gt=0)
    fixture: int = Field(gt=0)
    round: int = Field(ge=1, le=50)
    team: str
    opponent_team: str
    was_home: bool
    minutes: int = Field(ge=0, le=130)
    total_points: int = Field(ge=-15, le=50)
    price: float = Field(ge=0.3, le=20.0)   # 2024-25 : "assistant managers" à £0.5m+
    goals_scored: int = Field(ge=0, le=10)
    assists: int = Field(ge=0, le=10)

    @model_validator(mode="after")
    def check_teams(self) -> "FplHistoryRecord":
        if self.team == self.opponent_team:
            raise ValueError("équipe = adversaire")
        return self


def validate_frame(df: pd.DataFrame, model: type[BaseModel]) -> tuple[pd.DataFrame, list[dict]]:
    """Valide ligne à ligne ; renvoie (lignes valides, rejets {payload, errors})."""
    if df.empty:
        return df, []
    records = df.astype(object).where(pd.notna(df), None).to_dict(orient="records")
    keep_mask: list[bool] = []
    rejects: list[dict] = []
    for rec in records:
        try:
            model.model_validate(rec)
            keep_mask.append(True)
        except ValidationError as exc:
            keep_mask.append(False)
            rejects.append({"payload": rec, "errors": exc.errors(include_url=False)})
    if rejects:
        log.warning("%s : %d/%d lignes rejetées", model.__name__, len(rejects), len(records))
    return df.loc[keep_mask].copy(), rejects
