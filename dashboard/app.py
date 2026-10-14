"""Tableau de bord Streamlit — Premier League 2016-17 -> 2026-27.

Hiérarchie visuelle (cf. docs/DESIGN_SPEC.md) :
  Niveau 1  KPIs (4 tuiles max)         -> "où en est-on ?"
  Niveau 2  Graphique principal         -> "comment en est-on arrivé là ?"
  Niveau 3  Tableaux détaillés          -> "quels sont les chiffres exacts ?"
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402
import plotly.graph_objects as go  # noqa: E402
import streamlit as st  # noqa: E402

from analytics import data_access as da  # noqa: E402
from analytics.betting import compare_to_market, home_advantage_by_season, implied_probabilities  # noqa: E402
from analytics.form import current_streaks, home_away_splits, team_form  # noqa: E402
from analytics.fpl_metrics import fixture_difficulty, player_metrics  # noqa: E402
from analytics.standings import rolling_table_by_matchday, table_at_date  # noqa: E402
from analytics.xg_model import fit_team_strengths, predict_fixtures  # noqa: E402
from config.settings import get_settings  # noqa: E402
from core.db import read_sql  # noqa: E402
from dashboard.design_tokens import (DIVERGING, LIGHT, SEQUENTIAL_BLUE,  # noqa: E402
                                     register_plotly_templates, team_color_map)

st.set_page_config(page_title="Premier League Analytics", page_icon="⚽", layout="wide")
register_plotly_templates()
TEMPLATE = "pl_light"
CURRENT = get_settings().current_season


# ------------------------------------------------------------------ données (cache)
@st.cache_data(ttl=900, show_spinner=False)
def load_seasons() -> pd.DataFrame:
    return read_sql("SELECT season_id, label, is_current FROM pl.dim_seasons ORDER BY season_id")


@st.cache_data(ttl=900, show_spinner="Chargement des matchs…")
def load_team_matches() -> pd.DataFrame:
    return da.team_matches()


@st.cache_data(ttl=900, show_spinner=False)
def load_matches(played: bool | None = True) -> pd.DataFrame:
    return da.matches(played=played)


@st.cache_data(ttl=900, show_spinner=False)
def load_fpl(season_id: int) -> pd.DataFrame:
    return da.fpl_player_gameweeks(season_id)


@st.cache_data(ttl=900, show_spinner=False)
def load_referees(season_id: int) -> pd.DataFrame:
    return read_sql("SELECT * FROM pl.mv_referee_profile WHERE season_id = :s ORDER BY matches DESC",
                    {"s": season_id})


# ---------------------------------------------------------------------- composants
def kpi_row(items: list[tuple[str, str, str | None]]) -> None:
    """Tuiles KPI : libellé, valeur, delta vs saison précédente (le chiffre EST le graphique)."""
    for col, (label, value, delta) in zip(st.columns(len(items)), items):
        col.metric(label, value, delta)


def fig_layout(fig: go.Figure, title: str, height: int = 420) -> go.Figure:
    fig.update_layout(template=TEMPLATE, height=height,
                      title=dict(text=title, font=dict(size=15, color=LIGHT["ink"]), x=0))
    return fig


def bump_chart(rt: pd.DataFrame, selected: list[str]) -> go.Figure:
    teams = sorted(rt["team_name"].unique())
    colors = team_color_map(selected, teams)
    fig = go.Figure()
    # Contexte d'abord (gris, fin), sélection ensuite (au-dessus, 2px + étiquette directe)
    for team in sorted(teams, key=lambda t: t in selected):
        d = rt[rt["team_name"] == team]
        is_sel = team in selected
        fig.add_trace(go.Scatter(
            x=d["matchday"], y=d["position"], mode="lines", name=team, showlegend=is_sel,
            line=dict(color=colors[team], width=2 if is_sel else 1),
            opacity=1 if is_sel else 0.6,
            hovertemplate=f"<b>{team}</b><br>Journée %{{x}} · %{{y}}<sup>e</sup><extra></extra>",
        ))
        if is_sel and not d.empty:
            last = d.iloc[-1]
            fig.add_annotation(x=last["matchday"], y=last["position"], text=f" {last['short_name']}",
                               showarrow=False, xanchor="left", font=dict(color=LIGHT["ink_2"], size=11))
    fig.update_yaxes(autorange="reversed", dtick=1, title="Position")
    fig.update_xaxes(title="Journée (matchs joués)")
    return fig_layout(fig, "Évolution du classement", 480)


def xg_scatter(table: pd.DataFrame, selected: list[str]) -> go.Figure:
    colors = team_color_map(selected, table["team_name"].tolist())
    fig = go.Figure()
    for r in table.sort_values("team_name", key=lambda s: s.isin(selected)).itertuples():
        sel = r.team_name in selected
        fig.add_trace(go.Scatter(
            x=[r.xg_for / r.played], y=[r.xg_against / r.played], mode="markers+text" if sel else "markers",
            text=[r.short_name] if sel else None, textposition="top center",
            textfont=dict(color=LIGHT["ink_2"]),
            marker=dict(size=12 if sel else 9, color=colors[r.team_name], line=dict(color=LIGHT["surface"], width=2)),
            name=r.team_name, showlegend=False,
            hovertemplate=(f"<b>{r.team_name}</b><br>xG pour/match %{{x:.2f}}<br>"
                           f"xG contre/match %{{y:.2f}}<br>Points {r.points}<extra></extra>"),
        ))
    fig.update_yaxes(autorange="reversed", title="xG concédés / match (↑ = meilleure défense)")
    fig.update_xaxes(title="xG créés / match")
    return fig_layout(fig, "Profil offensif / défensif (xG)")


# --------------------------------------------------------------------------- layout
seasons = load_seasons()
if seasons.empty:
    st.error("Base vide : lancez `python -m pipelines.cli init-db` puis `backfill`.")
    st.stop()

st.title("Premier League Analytics")
st.caption("10 saisons + saison en cours · football-data.co.uk, API FPL, archive FPL open-source")

tm_all = load_team_matches()
if tm_all.empty:
    st.warning("Aucun match chargé pour l'instant.")
    st.stop()

# Filtres globaux : une seule rangée, au-dessus des graphiques
labels = dict(zip(seasons["label"], seasons["season_id"]))
f1, f2 = st.columns([1, 3])
season_label = f1.selectbox("Saison", list(labels), index=len(labels) - 1)
season_id = labels[season_label]
tm = tm_all[tm_all["season_id"] == season_id]
teams_in_season = sorted(tm["team_name"].unique())
selected = f2.multiselect("Équipes mises en avant (3 max)", teams_in_season,
                          default=teams_in_season[:0], max_selections=3)

tab_season, tab_team, tab_ref, tab_fpl = st.tabs(["Saison", "Équipe", "Arbitres & marché", "FPL Scout"])

# ================================================================ SAISON
with tab_season:
    table = table_at_date(tm, season_id, tm["match_date"].max())
    prev = tm_all[tm_all["season_id"] == season_id - 1]
    played_matches = tm[tm["is_home"]]
    gpm = (played_matches["goals_for"] + played_matches["goals_against"]).mean()
    hw = (played_matches["result"] == "W").mean()
    prev_home = prev[prev["is_home"]]
    d_gpm = gpm - (prev_home["goals_for"] + prev_home["goals_against"]).mean() if not prev.empty else None
    d_hw = hw - (prev_home["result"] == "W").mean() if not prev.empty else None
    leader = table.iloc[0]
    kpi_row([
        ("Leader", f"{leader['team_name']}", f"{leader['points']} pts · {leader['played']} J"),
        ("Matchs joués", f"{len(played_matches)}", None),
        ("Buts / match", f"{gpm:.2f}", f"{d_gpm:+.2f} vs N-1" if d_gpm is not None else None),
        ("Victoires à domicile", f"{hw:.0%}", f"{d_hw:+.1%} vs N-1" if d_hw is not None else None),
    ])

    c1, c2 = st.columns([3, 2])
    c1.plotly_chart(bump_chart(rolling_table_by_matchday(tm), selected), use_container_width=True)
    c2.plotly_chart(xg_scatter(table, selected), use_container_width=True)

    form = team_form(tm).sort_values("match_date").groupby("team_name").tail(1)[["team_name", "form_string"]]
    show = table.merge(form, on="team_name", how="left")
    st.subheader("Classement")
    st.dataframe(
        show[["position", "team_name", "played", "won", "drawn", "lost", "goals_for", "goals_against",
              "goal_diff", "points", "ppg", "xg_diff", "form_string"]],
        hide_index=True, use_container_width=True,
        column_config={
            "position": st.column_config.NumberColumn("#", width="small"),
            "team_name": "Équipe", "played": "J", "won": "G", "drawn": "N", "lost": "P",
            "goals_for": "BP", "goals_against": "BC", "goal_diff": "Diff",
            "points": st.column_config.ProgressColumn("Pts", min_value=0, max_value=114, format="%d"),
            "ppg": st.column_config.NumberColumn("Pts/match", format="%.2f"),
            "xg_diff": st.column_config.NumberColumn("xG diff", format="%+.1f",
                                                     help="xG pour − xG contre (réel 2022+, proxy avant)"),
            "form_string": st.column_config.TextColumn("Forme (5 derniers)", help="W = victoire, D = nul, L = défaite"),
        },
    )

# ================================================================= ÉQUIPE
with tab_team:
    team = st.selectbox("Équipe", teams_in_season, index=0, key="team_pick")
    f = team_form(tm)
    tf = f[f["team_name"] == team]
    streaks = current_streaks(tm)
    s = streaks[streaks["team_name"] == team].iloc[0]
    kpi_row([
        ("Points / match (EWMA)", f"{tf['ppg_ewm'].iloc[-1]:.2f}", f"{tf['momentum'].iloc[-1]:+.2f} momentum"),
        ("Série d'invincibilité", f"{s['unbeaten']}", None),
        ("Série sans victoire", f"{s['winless']}", None),
        ("Clean sheets consécutifs", f"{s['clean_sheets']}", None),
    ])

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=tf["match_date"], y=tf["ppg_last5"], mode="lines+markers", name="Pts/match (5 derniers)",
                             line=dict(color=LIGHT["categorical"][0], width=2), marker=dict(size=8),
                             customdata=tf[["opponent_name", "goals_for", "goals_against"]],
                             hovertemplate="%{x|%d %b %Y}<br>vs %{customdata[0]} %{customdata[1]}-%{customdata[2]}"
                                           "<br>Forme %{y:.2f}<extra></extra>"))
    fig.add_hline(y=tf["points"].mean(), line=dict(color=LIGHT["axis"], width=1),
                  annotation_text="moyenne de saison", annotation_font_color=LIGHT["muted"])
    fig.update_yaxes(range=[0, 3], title="Points par match")
    st.plotly_chart(fig_layout(fig, f"{team} — forme glissante"), use_container_width=True)

    # xG différentiel par match : divergent (bleu = domination, rouge = dominé)
    if tf["xgd"].notna().any():
        colors = [DIVERGING["pos"] if v >= 0 else DIVERGING["neg"] for v in tf["xgd"].fillna(0)]
        fig = go.Figure(go.Bar(x=tf["match_date"], y=tf["xgd"], marker=dict(color=colors, line=dict(width=0)),
                               customdata=tf[["opponent_name"]],
                               hovertemplate="vs %{customdata[0]}<br>xG diff %{y:+.2f}<extra></extra>"))
        fig.update_yaxes(title="xG pour − xG contre")
        st.plotly_chart(fig_layout(fig, "Domination par match (xG différentiel)", 320), use_container_width=True)

    st.dataframe(home_away_splits(tm[tm["team_name"] == team]), hide_index=True, use_container_width=True)

# ======================================================= ARBITRES & MARCHÉ
with tab_ref:
    m_all = load_matches()
    ha = home_advantage_by_season(m_all)
    ha["label"] = ha["season_id"].map(dict(zip(seasons["season_id"], seasons["label"])))
    fig = go.Figure()
    for i, (col, name) in enumerate((("home_win", "Victoire domicile"), ("draw", "Nul"), ("away_win", "Victoire extérieur"))):
        fig.add_trace(go.Scatter(x=ha["label"], y=ha[col], mode="lines+markers", name=name,
                                 line=dict(width=2, color=LIGHT["categorical"][i]), marker=dict(size=8),
                                 hovertemplate=f"{name} %{{y:.1%}}<extra></extra>"))
    if "2020-21" in ha["label"].values:
        fig.add_annotation(x="2020-21", y=float(ha.loc[ha["label"] == "2020-21", "away_win"].iloc[0]),
                           text="Huis clos", showarrow=True, arrowcolor=LIGHT["axis"],
                           font=dict(color=LIGHT["ink_2"], size=11))
    fig.update_yaxes(tickformat=".0%", title="Part des matchs")
    fig.update_layout(hovermode="x unified")
    st.plotly_chart(fig_layout(fig, "Avantage du terrain par saison"), use_container_width=True)

    st.subheader("Profil des arbitres")
    st.dataframe(load_referees(season_id), hide_index=True, use_container_width=True,
                 column_config={"referee_id": None, "season_id": None})

    st.subheader("Modèle vs marché (saison sélectionnée)")
    ms = m_all[m_all["season_id"] == season_id]
    hist = m_all[m_all["match_date"] < ms["match_date"].min()] if not ms.empty else m_all
    if len(hist) > 300 and not ms.empty:
        strengths = fit_team_strengths(hist)
        known = ms[ms["home_team"].isin(strengths.attack.index) & ms["away_team"].isin(strengths.attack.index)]
        preds = implied_probabilities(predict_fixtures(strengths, known))
        st.dataframe(compare_to_market(preds), use_container_width=True)
        st.caption("Brier / log-loss plus bas = meilleur. Modèle figé en début de saison (pas de mise à jour en cours).")
    else:
        st.info("Historique insuffisant pour entraîner le modèle sur cette saison.")

# =============================================================== FPL SCOUT
with tab_fpl:
    gw = load_fpl(season_id)
    if gw.empty:
        st.info("Pas de données FPL pour cette saison.")
    else:
        last_gw = int(gw["gw_number"].max())
        c1, c2, c3 = st.columns([1, 1, 2])
        as_of = c1.slider("Jusqu'à la GW", 1, last_gw, last_gw)
        pos = c2.multiselect("Poste", ["GKP", "DEF", "MID", "FWD"], default=["GKP", "DEF", "MID", "FWD"])
        price_max = c3.slider("Prix max (£m)", 3.5, 15.5, 15.5, 0.5)
        pm = player_metrics(gw, as_of)
        view = pm[pm["position"].isin(pos) & (pm["price"] <= price_max) & pm["eligible"]]

        top = view.iloc[0] if not view.empty else None
        kpi_row([
            ("Meilleur scoreur", f"{top['web_name']}" if top is not None else "—",
             f"{int(top['points'])} pts" if top is not None else None),
            ("Meilleur rapport pts/£m", view.sort_values("points_per_million").iloc[-1]["web_name"] if not view.empty else "—", None),
            ("Joueurs éligibles", f"{len(view)}", None),
            ("GW analysée", f"{as_of}/{last_gw}", None),
        ])

        # Prix vs points : une série, emphase sur le top 5 en valeur (étiquettes directes sélectives)
        best = set(view.nlargest(5, "points_per_million")["player_id"])
        fig = go.Figure(go.Scatter(
            x=view["price"], y=view["points"], mode="markers",
            marker=dict(size=9, color=[LIGHT["categorical"][0] if p in best else LIGHT["context"] for p in view["player_id"]],
                        line=dict(color=LIGHT["surface"], width=2)),
            customdata=view[["web_name", "team", "form", "xgi_per_90"]],
            hovertemplate="<b>%{customdata[0]}</b> (%{customdata[1]})<br>£%{x:.1f}m · %{y} pts"
                          "<br>Forme %{customdata[2]:.1f} · xGI/90 %{customdata[3]:.2f}<extra></extra>"))
        for r in view[view["player_id"].isin(best)].itertuples():
            fig.add_annotation(x=r.price, y=r.points, text=r.web_name, showarrow=False, yshift=12,
                               font=dict(size=11, color=LIGHT["ink_2"]))
        fig.update_xaxes(title="Prix (£m)")
        fig.update_yaxes(title="Points cumulés")
        st.plotly_chart(fig_layout(fig, "Valeur : prix vs points (top 5 pts/£m en bleu)"), use_container_width=True)

        st.dataframe(
            view[["web_name", "team", "position", "price", "points", "form", "points_per_million",
                  "xgi_per_90", "minutes_security", "vorp", "transfer_momentum"]].head(50),
            hide_index=True, use_container_width=True,
            column_config={
                "web_name": "Joueur", "team": "Équipe", "position": "Poste",
                "price": st.column_config.NumberColumn("Prix", format="£%.1fm"),
                "form": st.column_config.NumberColumn("Forme", format="%.1f", help="Moyenne des 5 dernières GW"),
                "points_per_million": st.column_config.NumberColumn("Pts/£m", format="%.1f"),
                "xgi_per_90": st.column_config.NumberColumn("xGI/90", format="%.2f"),
                "minutes_security": st.column_config.ProgressColumn("Temps de jeu", min_value=0, max_value=1),
                "vorp": st.column_config.NumberColumn("VORP", help="Points au-dessus d'un remplaçant du même poste"),
                "transfer_momentum": st.column_config.NumberColumn("Momentum transferts", format="%+.3f"),
            })

        # Calendrier : heatmap FDR (séquentiel une teinte, valeur écrite dans la cellule)
        if season_id == CURRENT:
            upcoming = load_matches(played=False)
            upcoming = upcoming[upcoming["season_id"] == season_id].dropna(subset=["gw_number"])
            played = load_matches()
            if not upcoming.empty and len(played) > 300:
                detail, summary = fixture_difficulty(upcoming, fit_team_strengths(played), horizon=6)
                gws = sorted(detail["gw_number"].unique())[:6]
                d = detail[detail["gw_number"].isin(gws)]
                order = summary["team"].tolist()
                pivot = d.pivot_table(index="team", columns="gw_number", values="fdr", aggfunc="mean").reindex(order)
                opp = d.assign(lbl=d["opponent"].str[:3].str.upper() + d["is_home"].map({True: " (D)", False: " (E)"})) \
                       .pivot_table(index="team", columns="gw_number", values="lbl", aggfunc="first").reindex(order)
                fig = go.Figure(go.Heatmap(
                    z=pivot.values, x=[f"GW{int(c)}" for c in pivot.columns], y=pivot.index,
                    text=opp.values, texttemplate="%{text}", zmin=1, zmax=5, xgap=2, ygap=2,
                    colorscale=[[i / 6, c] for i, c in enumerate(SEQUENTIAL_BLUE)],
                    colorbar=dict(title="FDR", tickvals=[1, 3, 5], ticktext=["Facile", "Moyen", "Difficile"]),
                    hovertemplate="%{y} · %{x}<br>%{text}<br>Difficulté %{z:.1f}/5<extra></extra>"))
                fig.update_yaxes(autorange="reversed")
                st.plotly_chart(fig_layout(fig, "Difficulté des 6 prochaines GW (modèle, équipes triées du plus facile au plus dur)", 620),
                                use_container_width=True)
