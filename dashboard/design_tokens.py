"""Design tokens du tableau de bord (source unique pour Streamlit, Plotly et Power BI).

Règles (cf. docs/DESIGN_SPEC.md) :
  - Couleurs catégorielles attribuées dans un ORDRE FIXE, par entité (jamais par rang).
  - Pas de couleurs de clubs pour les séries : 20 clubs dont ~8 rouges et ~6 bleus seraient
    indiscernables (et non accessibles aux daltoniens). On met en avant ≤ 3 équipes
    sélectionnées (slots 1-3), les autres en gris neutre.
  - Séquentiel = une seule teinte (bleu) clair -> foncé. Divergent = bleu <-> rouge, milieu gris.
  - Couleurs de statut réservées (bon / alerte / critique), toujours accompagnées d'icône + libellé.
"""
from __future__ import annotations

import plotly.graph_objects as go
import plotly.io as pio

from skills.registry import skill

FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'

# Palette catégorielle validée (daltonisme : ΔE adjacent ≥ 8 ; scatter : 3 premiers slots seulement)
CATEGORICAL_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
CATEGORICAL_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]

SEQUENTIAL_BLUE = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]
DIVERGING = {"neg": "#e34948", "mid_light": "#f0efec", "mid_dark": "#383835", "pos": "#2a78d6"}
STATUS = {"good": "#0ca30c", "warning": "#fab219", "serious": "#ec835a", "critical": "#d03b3b"}

LIGHT = {
    "surface": "#fcfcfb", "page": "#f9f9f7", "ink": "#0b0b0b", "ink_2": "#52514e",
    "muted": "#898781", "grid": "#e1e0d9", "axis": "#c3c2b7", "context": "#c3c2b7",
    "categorical": CATEGORICAL_LIGHT,
}
DARK = {
    "surface": "#1a1a19", "page": "#0d0d0d", "ink": "#ffffff", "ink_2": "#c3c2b7",
    "muted": "#898781", "grid": "#2c2c2a", "axis": "#383835", "context": "#4a4a47",
    "categorical": CATEGORICAL_DARK,
}

# Résultats de match : codage double (couleur + lettre), jamais la couleur seule
RESULT_COLORS = {"W": STATUS["good"], "D": "#898781", "L": STATUS["critical"]}


@skill("design.plotly_template", "design", "Enregistre le template Plotly 'pl_light' / 'pl_dark'")
def register_plotly_templates() -> None:
    for name, t in (("pl_light", LIGHT), ("pl_dark", DARK)):
        axis = dict(gridcolor=t["grid"], linecolor=t["axis"], zeroline=False, ticks="",
                    tickfont=dict(color=t["muted"], size=11), title_font=dict(color=t["ink_2"], size=12))
        pio.templates[name] = go.layout.Template(layout=dict(
            font=dict(family=FONT, color=t["ink"], size=13),
            paper_bgcolor=t["surface"], plot_bgcolor=t["surface"],
            colorway=t["categorical"],
            xaxis=axis, yaxis=axis,
            legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, font=dict(color=t["ink_2"])),
            hoverlabel=dict(bgcolor=t["surface"], font=dict(family=FONT, color=t["ink"]), bordercolor=t["axis"]),
            margin=dict(l=8, r=8, t=40, b=8),
            bargap=0.25,
        ))


def team_color_map(selected: list[str], all_teams: list[str], mode: str = "light") -> dict[str, str]:
    """Emphase : équipes sélectionnées -> slots 1..3 (ordre de sélection), les autres en gris.
    La couleur suit l'équipe : désélectionner une équipe ne repeint pas les autres."""
    t = LIGHT if mode == "light" else DARK
    colors = {team: t["context"] for team in all_teams}
    for i, team in enumerate(selected[:3]):
        colors[team] = t["categorical"][i]
    return colors


@skill("design.palette_report", "design", "Résumé des tokens de design (pour Power BI / documentation)")
def palette_report() -> dict:
    return {"categorical_light": CATEGORICAL_LIGHT, "categorical_dark": CATEGORICAL_DARK,
            "sequential": SEQUENTIAL_BLUE, "diverging": DIVERGING, "status": STATUS,
            "surfaces": {"light": LIGHT["surface"], "dark": DARK["surface"]}}
