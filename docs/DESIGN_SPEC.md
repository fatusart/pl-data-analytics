# Spécification UI/UX — Premier League Analytics

## 1. Principes

| Principe | Application concrète |
|---|---|
| **Une question par écran** | Saison = "qui gagne et pourquoi" · Équipe = "dans quelle dynamique" · Arbitres & marché = "quels biais structurels" · FPL = "qui acheter" |
| **Hiérarchie en 3 niveaux** | ① 4 tuiles KPI max → ② 1-2 graphiques principaux → ③ tableau détaillé (le chiffre exact) |
| **Filtres sur une seule rangée, au-dessus** | Saison + équipes mises en avant ; filtres locaux (poste, prix, GW) en tête d'onglet |
| **Emphase plutôt qu'arc-en-ciel** | 20 équipes ne se distinguent pas en couleur → 3 équipes mises en avant max, les 17 autres en gris de contexte |
| **Jamais la couleur seule** | Résultats W/D/L écrits en lettres ; FDR écrit dans la cellule ; légende + étiquettes directes |
| **Un seul axe Y** | Aucun graphique à double axe ; deux mesures d'échelles différentes = deux graphiques |

## 2. Palette (tokens dans `dashboard/design_tokens.py`)

| Rôle | Clair | Sombre | Usage |
|---|---|---|---|
| Surface graphique | `#fcfcfb` | `#1a1a19` | fond des graphiques |
| Encre principale / secondaire | `#0b0b0b` / `#52514e` | `#ffffff` / `#c3c2b7` | titres, valeurs / libellés |
| Grille / axe | `#e1e0d9` / `#c3c2b7` | `#2c2c2a` / `#383835` | traits fins, pleins (jamais pointillés) |
| Catégoriel 1→3 | `#2a78d6` `#eb6834` `#1baf7a` | `#3987e5` `#d95926` `#199e70` | équipes mises en avant, dans l'ordre de sélection |
| Contexte | `#c3c2b7` | `#4a4a47` | équipes non sélectionnées |
| Séquentiel | bleu `#cde2fb` → `#0d366b` | idem | heatmap FDR, intensités |
| Divergent | rouge `#e34948` ↔ gris ↔ bleu `#2a78d6` | | xG différentiel (dominé ↔ dominant) |
| Statut | bon `#0ca30c` · alerte `#fab219` · critique `#d03b3b` | | blessures / disponibilité FPL, toujours avec icône + libellé |

Pourquoi pas les couleurs des clubs : Arsenal, Liverpool, Man Utd, Forest, Southampton, Bournemouth, Brentford, Sunderland sont rouges ; Chelsea, Everton, Man City, Brighton, Leicester, Ipswich sont bleus. Un daltonien (≈ 8 % des hommes) ne les distinguerait pas. Les couleurs de club peuvent apparaître en **pastille d'identité** à côté du nom, jamais comme couleur de série.

La palette catégorielle est validée pour le daltonisme : ΔE ≥ 8 entre slots adjacents (lignes, barres). Pour les nuages de points (toutes paires en concurrence), seuls les **3 premiers slots** passent : d'où la limite de 3 équipes mises en avant.

## 3. Catalogue des vues

### Onglet Saison
| Zone | Visuel | Justification |
|---|---|---|
| KPI | Leader (pts, J) · Matchs joués · Buts/match (Δ N-1) · % victoires domicile (Δ N-1) | un seul chiffre = une tuile, pas un graphique |
| Principal | **Bump chart** position × journée | la trajectoire raconte la saison mieux que le classement final ; axe Y inversé (1er en haut) |
| Secondaire | **Nuage xG créés vs xG concédés / match** | quadrants lisibles : en haut à droite = dominant ; Y inversé pour que "haut = bon" |
| Détail | Tableau classement + barre de progression points + chaîne de forme `WWDLW` | lecture exacte, tri natif |

### Onglet Équipe
- KPI : PPG pondéré (EWMA) + momentum, séries en cours (invincibilité, sans victoire, clean sheets).
- Ligne de forme glissante 5 matchs, ligne de référence "moyenne de saison" (trait fin neutre).
- Barres divergentes de xG différentiel par match (bleu = domination, rouge = dominé).
- Tableau domicile / extérieur.

### Onglet Arbitres & marché
- Lignes Victoire domicile / Nul / Victoire extérieur par saison, annotation "Huis clos" sur 2020-21.
- Tableau profil arbitre (cartons/match, fautes/match, % victoires domicile, écart cartons ext.-dom.).
- Tableau de calibration modèle vs marché (Brier, log-loss).

### Onglet FPL Scout
- Filtres : GW de référence, poste, prix max.
- KPI : meilleur scoreur, meilleur rapport pts/£m, nb de joueurs éligibles (≥ 270 min), GW analysée.
- Nuage prix × points (une série ; top 5 pts/£m en bleu + étiquette directe, reste en gris).
- Tableau scout : forme, pts/£m, xGI/90, sécurité de temps de jeu (barre), VORP, momentum transferts.
- Heatmap FDR modélisée 20 équipes × 6 GW (séquentiel bleu, adversaire + D/E écrit dans la cellule, équipes triées du calendrier le plus facile au plus dur).

## 4. Interaction
- Infobulle sur **chaque** marque (hover Plotly) : entité, valeur formatée, contexte (adversaire, score).
- `hovermode="x unified"` sur les séries temporelles multiples.
- La couleur suit l'équipe : retirer une équipe de la sélection ne repeint pas les autres.
- Mise en cache des requêtes 15 min (`st.cache_data(ttl=900)`).

## 5. Transposition Power BI
- Connecteur PostgreSQL → importer `pl.*` (dims + faits) ; relations en étoile sur `*_id` (cardinalité 1-N, filtre unidirectionnel dimension → fait).
- Marquer `dim_dates` comme table de dates.
- Thème JSON : `dataColors` = les 8 couleurs catégorielles dans l'ordre, `background` = `#fcfcfb`, `foreground` = `#0b0b0b`.
- Mesures DAX de base :

```DAX
Points :=
SUMX ( fact_matches, SWITCH ( fact_matches[ftr], "H", 3, "D", 1, 0 ) )   -- perspective domicile ; utiliser v_team_match pour l'équipe

Buts par match :=
DIVIDE ( SUM ( fact_matches[fthg] ) + SUM ( fact_matches[ftag] ), COUNTROWS ( fact_matches ) )

% Victoires domicile :=
DIVIDE ( CALCULATE ( COUNTROWS ( fact_matches ), fact_matches[ftr] = "H" ), COUNTROWS ( fact_matches ) )

Points FPL par £m :=
DIVIDE ( SUM ( fact_fpl_history[total_points] ), AVERAGE ( fact_fpl_history[price] ) )
```

- Mise en page 16:9 : bandeau KPI (hauteur 15 %), visuel principal (55 %, 2/3 largeur) + secondaire (1/3), tableau en bas ; segments (slicers) alignés sur une rangée en haut.
