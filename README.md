# Premier League Data Platform (2016-17 → 2026-27)

Plateforme de Data Engineering : ingestion multi-sources, modèle en étoile PostgreSQL,
analyses statistiques/prédictives et tableau de bord Streamlit, sur les 10 saisons closes
(2016-17 → 2025-26) et la saison en cours 2026-27, mise à jour de façon incrémentale.

## Architecture

```
            ┌──────────── EXTRACT (skills "retrieval") ─────────────┐
 football-data.co.uk ──┐                                            │
 API FPL officielle ───┤  HttpClient : token bucket par source,     │   data/raw/<source>/<saison>/
 Archive FPL (vaastav) ┤  retries exponentiels + jitter, Retry-After├──► zone brute immuable
 FPL-Core-Insights* ───┤  requêtes conditionnelles (ETag), pagination  (+ .meta.json : md5, ETag)
 API-Football* ────────┘                                            │
            └───────────────────────────────────────────────────────┘
                                   │
            ┌──────────── TRANSFORM (skills "transform") ────────────┐
            │ normalisation équipes (table d'alias) / joueurs (code   │
            │ FPL stable) / arbitres · dédoublonnage · contrats       │──► lignes invalides
            │ Pydantic · record_hash md5                              │    -> meta.rejected_rows
            └─────────────────────────────────────────────────────────┘
                                   │
            ┌──────────── LOAD (skills "load") ──────────────────────┐
            │ upsert ON CONFLICT par clé naturelle, mis à jour        │──► PostgreSQL : schéma pl
            │ seulement si record_hash change · watermarks            │    (étoile) + meta (pilotage)
            └─────────────────────────────────────────────────────────┘
                                   │
            ANALYTICS (skills "analysis")  ──►  DASHBOARD Streamlit (skills "design")
   classements roulants · forme · xG proxy ·         KPIs · bump chart · xG · FDR · scout FPL
   forces Poisson · métriques FPL · marché
                                                          * = optionnel
```

## Arborescence

```
pl-data-platform/
├── docker-compose.yml        # postgres, etl (à la demande), scheduler, dashboard, pgadmin
├── Dockerfile
├── requirements.txt
├── .env.example
├── config/
│   ├── settings.py           # pydantic-settings (variables PL_*)
│   ├── sources.yaml          # URLs, gabarits par saison, rate limits
│   ├── team_mapping.csv      # table de correspondance des noms d'équipes
│   └── venues.csv            # stades par équipe et période (Spurs à Wembley, Everton 2025…)
├── sql/
│   ├── 01_schema.sql         # DDL étoile + meta + seed saisons/calendrier
│   └── 02_views.sql          # v_team_match, mv_league_table, mv_referee_profile, v_fpl_player_gameweek
├── core/                     # http (rate limit/retry), db (upsert, journal, watermarks), seasons
├── skills/registry.py        # registre des compétences (décorateur @skill)
├── extractors/               # football_data, fpl_api, fpl_history, fpl_core_insights, api_football
├── transformers/             # normalization, schemas (Pydantic), matches, fpl
├── loaders/postgres_loader.py
├── analytics/                # standings, form, xg_model, fpl_metrics, betting, data_access
├── pipelines/                # cli (point d'entrée), etl (backfill/incrémental), scheduler
├── dashboard/                # app.py (Streamlit), design_tokens.py
├── docs/DESIGN_SPEC.md       # spécifications UI/UX + transposition Power BI
├── .streamlit/config.toml
└── tests/
```

## Modèle de données (schéma `pl`)

| Table | Grain | Clé naturelle |
|---|---|---|
| `dim_seasons` | saison (id = année de début) | `season_id` |
| `dim_dates` | jour | `date_id` (AAAAMMJJ) |
| `dim_teams` / `dim_team_aliases` | équipe canonique / libellé source | `team_name` / `alias` |
| `bridge_team_season` | équipe × saison (stade, id FPL saisonnier) | `(team_id, season_id)` |
| `dim_venues`, `dim_referees` | stade, arbitre | nom |
| `dim_gameweeks` | GW × saison | `(season_id, gw_number)` |
| `dim_players` | joueur (stable inter-saisons) | `fpl_code` |
| `dim_player_season` | joueur × saison (poste, prix, équipe) | `(player_id, season_id)` |
| `fact_matches` | match | `(season_id, home_team_id, away_team_id)` |
| `fact_player_match_stats` | joueur × match (performance) | `(player_id, match_id)` |
| `fact_fpl_history` | joueur × fixture FPL (points, prix, sélection) | `(player_id, season_id, fpl_fixture_id)` |
| `fact_fpl_player_snapshot` | joueur × jour (saison en cours) | `(player_id, snapshot_date)` |

Schéma `meta` : `etl_runs` (journal d'exécution), `etl_watermarks` (progression incrémentale),
`rejected_rows` (quarantaine JSONB).

## Installation

### Option A — Docker (recommandé)

```bash
cp .env.example .env
```

```bash
docker compose up -d postgres
```

```bash
docker compose run --rm etl backfill
```

```bash
docker compose up -d dashboard
```

Le dashboard est sur http://localhost:8501. Le schéma est créé automatiquement au premier
démarrage du volume (scripts `sql/` montés dans `docker-entrypoint-initdb.d`).

Mise à jour automatique de la saison en cours :

```bash
docker compose --profile scheduler up -d scheduler
```

pgAdmin (optionnel) : `docker compose --profile tools up -d pgadmin` → http://localhost:5050.

### Option B — Python local (3.11+)

```bash
python -m venv .venv
```

```bash
pip install -r requirements.txt
```

Puis, avec PostgreSQL accessible selon `.env` (activer le venv au préalable) :

```bash
python -m pipelines.cli init-db
```

```bash
python -m pipelines.cli backfill
```

```bash
streamlit run dashboard/app.py
```

## Ordre d'exécution

| # | Commande | Rôle |
|---|---|---|
| 1 | `init-db` | DDL idempotent + seed saisons 2016-2026 et calendrier (inutile sous Docker au 1er démarrage) |
| 2 | `backfill` | référentiels → matchs (football-data) → FPL historique → FPL saison courante → xG proxy + vues |
| 3 | `incremental` | à planifier : saison courante uniquement |
| — | `derive` | recalcul xG proxy + `REFRESH MATERIALIZED VIEW` |
| — | `backfill --from 2019-20 --to 2020-21 --force` | rechargement ciblé |
| — | `skills list` / `skills run <nom> --args '{...}'` | découverte / exécution d'une compétence |

Les matchs sont toujours chargés **avant** les données FPL : les faits FPL se rattachent à
`fact_matches` via (saison, domicile, extérieur), ce qui fournit aussi `gameweek_id` aux matchs.

## Stratégie de mise à jour

**Saisons closes (2016-17 → 2025-26) — batch figé.**
- Téléchargées une seule fois : un fichier brut présent n'est jamais re-téléchargé (sauf `--force`).
- Le md5 du fichier est mémorisé dans `meta.etl_watermarks` : relancer `backfill` ne retransforme rien.
- Le passage à une nouvelle saison se fait en changeant `PL_CURRENT_SEASON` (la saison précédente
  devient automatiquement figée) puis en insérant la ligne dans `dim_seasons`.

**Saison en cours (2026-27) — incrémental.**
1. football-data : requête conditionnelle (ETag/Last-Modified) ; si le md5 n'a pas changé, rien n'est rechargé.
2. FPL `bootstrap-static` + `fixtures` à chaque passage (2 requêtes) → snapshot quotidien prix/forme/sélection
   et insertion des fixtures à venir (`is_played = FALSE`, jamais écrasées si déjà chargées).
3. FPL `element-summary` (~750 requêtes à 2 req/s ≈ 6 min) **uniquement si une nouvelle GW est terminée**
   (`finished` et `data_checked`) depuis le watermark. La dernière GW déjà chargée est relue pour capter
   les corrections de bonus a posteriori. Si un appel échoue, le watermark n'avance pas.
4. Les upserts ne modifient que les lignes dont le `record_hash` a changé.
5. Cadence du `scheduler` : toutes les 2 h les jours de match, toutes les 12 h sinon.

**Fiabilité réseau.** Token bucket par source (football-data 0,5 req/s, FPL 2 req/s, GitHub 3 req/s,
API-Football 10 req/min), retries exponentiels avec jitter sur 408/429/5xx et erreurs de connexion,
respect de `Retry-After`, arrêt immédiat sur 401/403, 404 traité comme "absent" (fichier pas encore publié).

## Analyses disponibles

| Skill | Description |
|---|---|
| `analysis.rolling_table` / `table_at_date` | classement après N journées ou à une date (points, diff, buts marqués) |
| `analysis.team_form` / `streaks` / `home_away` | forme glissante, EWMA, momentum, séries, splits |
| `analysis.xg_proxy` | xG estimé par les tirs/tirs cadrés, calibré sur le xG FPL réel (2022-23+) pour une série homogène sur 10 ans |
| `analysis.team_strengths` / `predict_fixtures` | modèle de Poisson attaque/défense pondéré dans le temps → probabilités 1X2 |
| `analysis.fpl_player_metrics` | points/£m, forme, xGI/90, sécurité de temps de jeu, VORP, momentum de transferts |
| `analysis.fixture_difficulty` / `captaincy` | FDR modélisé sur N GW, score de capitanat |
| `analysis.implied_probs` / `calibration` / `home_advantage_trend` | marge bookmaker, Brier/log-loss modèle vs marché, effet huis clos |

## Limites connues
- **xG** : réel (somme des xG joueurs FPL) à partir de 2022-23 seulement ; avant, c'est le proxy modélisé (`home_xg_proxy`), signalé comme tel dans le dashboard.
- **Stades** : affectation à la saison (Tottenham a quitté Wembley en avril 2019 : la saison 2018-19 est rattachée au nouveau stade). Les capacités dans `venues.csv` sont indicatives.
- **Promus 2026-27** : un club absent de `team_mapping.csv` est créé automatiquement avec un avertissement ; ajouter ses alias au fichier.
- **FPL-Core-Insights** : désactivé par défaut, vérifier les chemins dans `sources.yaml` avant activation (la structure du dépôt évolue).
- **Formats des sources** : les colonnes de football-data et de l'archive FPL ont varié selon les saisons ; les transformations gèrent les variantes connues, et les lignes non conformes partent en quarantaine plutôt que de faire échouer le lot.

## Tests

```bash
python -m pytest -q tests
```
