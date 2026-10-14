import time
from pathlib import Path

import pandas as pd
import pytest

from core.http import TokenBucket
from core.seasons import Season, season_range
from transformers.normalization import TeamResolver, clean_referee, clean_vaastav_name, normalize_key


def test_season_codes():
    s = Season(2016)
    assert (s.label, s.fd_code, s.label_long) == ("2016-17", "1617", "2016-2017")
    assert Season(2099).label == "2099-00"
    assert Season.parse("2019-20") == Season.parse("1920") == Season.parse(2019) == Season(2019)


def test_season_range_covers_current():
    seasons = season_range()
    assert seasons[0] == Season(2016) and seasons[-1] == Season(2026)
    assert seasons[-1].is_current and not seasons[-1].is_frozen
    assert all(s.is_frozen for s in seasons[:-1])


def test_token_bucket_rate():
    bucket = TokenBucket(rate=20, burst=1)
    start = time.monotonic()
    for _ in range(6):
        bucket.acquire()
    assert time.monotonic() - start >= 0.2  # 5 attentes de 50 ms


@pytest.fixture
def resolver():
    return TeamResolver(pd.read_csv(Path(__file__).parents[1] / "config" / "team_mapping.csv"))


@pytest.mark.parametrize("raw,expected", [
    ("Man United", "Manchester United"), ("Man Utd", "Manchester United"),
    ("Spurs", "Tottenham Hotspur"), ("Nott'm Forest", "Nottingham Forest"),
    ("Wolves", "Wolverhampton Wanderers"), ("Brighton & Hove Albion", "Brighton & Hove Albion"),
    ("AFC Bournemouth", "Bournemouth"), ("Sheffield Utd", "Sheffield United"),
])
def test_team_aliases(resolver, raw, expected):
    assert resolver.resolve(raw) == expected


def test_unknown_team_is_created(resolver):
    assert resolver.resolve("Wrexham") == "Wrexham"
    assert len(resolver.short_name("Wrexham")) == 3


def test_name_cleaning():
    assert normalize_key("Søren Kragh-Andersen") == "soren kragh andersen"
    assert clean_vaastav_name("Aaron_Cresswell_402") == "Aaron Cresswell"
    assert clean_referee("Michael Oliver") == "M Oliver"
    assert clean_referee("M. Oliver") == "M Oliver"
    assert clean_referee(None) is None
