import pandas as pd

from analytics.form import team_form
from analytics.standings import rolling_table_by_matchday, table_at_date
from analytics.xg_model import fit_team_strengths
from core.seasons import Season
from transformers.matches import transform_football_data

CSV = """Div,Date,Time,HomeTeam,AwayTeam,FTHG,FTAG,FTR,HTHG,HTAG,HTR,Referee,HS,AS,HST,AST,HF,AF,HC,AC,HY,AY,HR,AR,B365H,B365D,B365A,AvgH,AvgD,AvgA
E0,16/08/2025,15:00,Man United,Spurs,2,1,H,1,0,H,Michael Oliver,14,9,6,3,10,12,5,4,1,2,0,0,1.9,3.6,4.2,1.88,3.55,4.1
E0,16/08/2025,17:30,Wolves,Nott'm Forest,0,0,D,0,0,D,A Taylor,8,7,2,2,11,9,3,6,2,1,0,0,2.5,3.2,2.9,2.45,3.2,2.95
E0,23/08/2025,15:00,Spurs,Wolves,3,0,A,1,0,H,A Taylor,15,4,7,1,8,10,7,1,0,3,0,1,1.5,4.3,6.5,1.5,4.2,6.4
E0,23/08/2025,15:00,Man United,Spurs,1,1,D,0,0,D,A Taylor,10,10,4,4,8,8,5,5,1,1,0,0,2.0,3.4,3.9,2.0,3.4,3.8
"""


def test_transform_football_data(tmp_path):
    p = tmp_path / "E0.csv"
    p.write_text(CSV, encoding="utf-8")
    df, rejects = transform_football_data(p, Season(2025))
    # ligne 3 : FTR incohérent (3-0 noté 'A') => rejetée ; ligne 4 : doublon d'affiche => dédoublonnée
    assert len(rejects) == 1
    assert len(df) == 2
    assert set(df["home_team"]) == {"Manchester United", "Wolverhampton Wanderers"}
    assert df.loc[df["home_team"] == "Manchester United", "ftr"].item() == "D"  # dernière occurrence gardée
    assert df["referee"].tolist() == ["A Taylor", "A Taylor"]
    assert str(df["kickoff_at"].dt.tz) == "UTC"
    assert df["record_hash"].str.len().eq(32).all()


def _team_matches():
    games = [("A", "B", 2, 0), ("C", "D", 1, 1), ("A", "C", 0, 1), ("B", "D", 3, 2), ("D", "A", 0, 0), ("B", "C", 1, 0)]
    rows = []
    for i, (h, a, hg, ag) in enumerate(games):
        date = pd.Timestamp("2025-08-01") + pd.Timedelta(days=7 * (i // 2))
        for team, opp, gf, ga, home in ((h, a, hg, ag, True), (a, h, ag, hg, False)):
            res = "W" if gf > ga else "D" if gf == ga else "L"
            rows.append(dict(match_id=i, season_id=2025, team_id=ord(team), team_name=team, short_name=team,
                             opponent_name=opp, is_home=home, match_date=date, goals_for=gf, goals_against=ga,
                             points={"W": 3, "D": 1, "L": 0}[res], result=res, xg_for=gf * 0.9, xg_against=ga * 0.9))
    return pd.DataFrame(rows)


def test_standings_and_form():
    tm = _team_matches()
    table = table_at_date(tm, 2025, "2025-12-31")
    assert table["points"].sum() == 3 * 4 + 2 * 2   # 4 victoires + 2 nuls
    assert table.iloc[0]["team_name"] == "B"        # 6 pts, meilleure diff que A/C
    rt = rolling_table_by_matchday(tm)
    assert rt.groupby("matchday")["position"].min().eq(1).all()
    f = team_form(tm, window=2)
    assert f.loc[f["team_name"] == "A", "form_string"].tolist() == ["W", "WL", "LD"]


def test_team_strengths_probabilities_sum_to_one():
    tm = _team_matches()
    m = tm[tm["is_home"]].rename(columns={"team_name": "home_team", "opponent_name": "away_team",
                                          "goals_for": "fthg", "goals_against": "ftag"})
    probs = fit_team_strengths(m).outcome_probs("A", "B")
    assert abs(probs["p_home"] + probs["p_draw"] + probs["p_away"] - 1) < 1e-3
