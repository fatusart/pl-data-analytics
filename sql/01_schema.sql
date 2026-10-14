-- =============================================================================
--  Premier League Data Platform — DDL (PostgreSQL 16)
--  Modèle en étoile : schéma `pl` (dimensions + faits), `meta` (pilotage ETL)
--  Script idempotent : peut être rejoué sans erreur (IF NOT EXISTS / ON CONFLICT).
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS pl;
CREATE SCHEMA IF NOT EXISTS meta;

SET search_path TO pl, public;

-- -----------------------------------------------------------------------------
--  DIMENSIONS
-- -----------------------------------------------------------------------------

-- Saison : clé "intelligente" = année de début (2016 => 2016-17)
CREATE TABLE IF NOT EXISTS pl.dim_seasons (
    season_id      SMALLINT    PRIMARY KEY CHECK (season_id BETWEEN 1992 AND 2100),
    label          VARCHAR(7)  NOT NULL UNIQUE,              -- '2016-17'
    start_date     DATE        NOT NULL,
    end_date       DATE        NOT NULL,
    is_current     BOOLEAN     NOT NULL DEFAULT FALSE,
    is_frozen      BOOLEAN     NOT NULL DEFAULT FALSE,       -- TRUE = saison close, chargée une fois
    CHECK (end_date > start_date)
);
-- Une seule saison "courante"
CREATE UNIQUE INDEX IF NOT EXISTS ux_dim_seasons_current
    ON pl.dim_seasons (is_current) WHERE is_current;

-- Calendrier (clé YYYYMMDD) — utile pour Power BI / agrégations temporelles
CREATE TABLE IF NOT EXISTS pl.dim_dates (
    date_id        INTEGER     PRIMARY KEY,                  -- 20170813
    calendar_date  DATE        NOT NULL UNIQUE,
    year           SMALLINT    NOT NULL,
    month          SMALLINT    NOT NULL,
    day            SMALLINT    NOT NULL,
    iso_week       SMALLINT    NOT NULL,
    day_of_week    SMALLINT    NOT NULL,                     -- 1 = lundi
    day_name       VARCHAR(9)  NOT NULL,
    is_weekend     BOOLEAN     NOT NULL,
    season_id      SMALLINT    REFERENCES pl.dim_seasons(season_id)
);

CREATE TABLE IF NOT EXISTS pl.dim_venues (
    venue_id       SMALLSERIAL PRIMARY KEY,
    venue_name     VARCHAR(80) NOT NULL UNIQUE,
    city           VARCHAR(60),
    capacity       INTEGER     CHECK (capacity > 0)
);

-- Équipe canonique (identité stable inter-saisons)
CREATE TABLE IF NOT EXISTS pl.dim_teams (
    team_id        SMALLSERIAL PRIMARY KEY,
    team_name      VARCHAR(60) NOT NULL UNIQUE,              -- 'Manchester United'
    short_name     CHAR(3)     NOT NULL UNIQUE,              -- 'MUN'
    fpl_team_code  INTEGER     UNIQUE,                       -- code FPL stable (≠ id saisonnier)
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Table de correspondance des libellés source -> équipe canonique
CREATE TABLE IF NOT EXISTS pl.dim_team_aliases (
    alias          VARCHAR(80) PRIMARY KEY,                  -- normalisé (minuscule, sans accent)
    team_id        SMALLINT    NOT NULL REFERENCES pl.dim_teams(team_id) ON DELETE CASCADE,
    source         VARCHAR(30)
);

-- Participation d'une équipe à une saison (pont équipe × saison)
CREATE TABLE IF NOT EXISTS pl.bridge_team_season (
    team_id        SMALLINT    NOT NULL REFERENCES pl.dim_teams(team_id),
    season_id      SMALLINT    NOT NULL REFERENCES pl.dim_seasons(season_id),
    fpl_team_id    SMALLINT,                                 -- id FPL (1..20) propre à la saison
    venue_id       SMALLINT    REFERENCES pl.dim_venues(venue_id),
    final_position SMALLINT    CHECK (final_position BETWEEN 1 AND 20),
    PRIMARY KEY (team_id, season_id),
    UNIQUE (season_id, fpl_team_id)
);

CREATE TABLE IF NOT EXISTS pl.dim_referees (
    referee_id     SMALLSERIAL PRIMARY KEY,
    referee_name   VARCHAR(60) NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS pl.dim_gameweeks (
    gameweek_id    SERIAL      PRIMARY KEY,
    season_id      SMALLINT    NOT NULL REFERENCES pl.dim_seasons(season_id),
    gw_number      SMALLINT    NOT NULL CHECK (gw_number BETWEEN 1 AND 50), -- 2019-20 : GW 39..47 (COVID)
    deadline_at    TIMESTAMPTZ,
    first_kickoff  TIMESTAMPTZ,
    last_kickoff   TIMESTAMPTZ,
    is_finished    BOOLEAN     NOT NULL DEFAULT FALSE,
    avg_points     NUMERIC(5,2),
    highest_points SMALLINT,
    UNIQUE (season_id, gw_number)
);

-- Joueur : identité stable via le `code` FPL (persistant d'une saison à l'autre)
CREATE TABLE IF NOT EXISTS pl.dim_players (
    player_id      SERIAL      PRIMARY KEY,
    fpl_code       INTEGER     NOT NULL UNIQUE,
    first_name     VARCHAR(80),
    second_name    VARCHAR(80),
    web_name       VARCHAR(60) NOT NULL,
    name_key       VARCHAR(160) NOT NULL,                    -- normalisé, pour le rapprochement
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_dim_players_name_key ON pl.dim_players (name_key);

-- Attributs saisonniers du joueur (SCD type 2 "par saison")
CREATE TABLE IF NOT EXISTS pl.dim_player_season (
    player_id      INTEGER     NOT NULL REFERENCES pl.dim_players(player_id),
    season_id      SMALLINT    NOT NULL REFERENCES pl.dim_seasons(season_id),
    fpl_element_id SMALLINT    NOT NULL,                     -- id FPL propre à la saison
    team_id        SMALLINT    REFERENCES pl.dim_teams(team_id), -- équipe en fin de saison / actuelle
    position       CHAR(3)     NOT NULL CHECK (position IN ('GKP','DEF','MID','FWD','MGR')),
    start_price    NUMERIC(4,1),
    current_price  NUMERIC(4,1),
    PRIMARY KEY (player_id, season_id),
    UNIQUE (season_id, fpl_element_id)
);

-- -----------------------------------------------------------------------------
--  FAITS
-- -----------------------------------------------------------------------------

-- Grain : 1 ligne = 1 match (football-data.co.uk + enrichissements)
CREATE TABLE IF NOT EXISTS pl.fact_matches (
    match_id        BIGSERIAL   PRIMARY KEY,
    season_id       SMALLINT    NOT NULL REFERENCES pl.dim_seasons(season_id),
    gameweek_id     INTEGER     REFERENCES pl.dim_gameweeks(gameweek_id),
    date_id         INTEGER     NOT NULL REFERENCES pl.dim_dates(date_id),
    kickoff_at      TIMESTAMPTZ,
    home_team_id    SMALLINT    NOT NULL REFERENCES pl.dim_teams(team_id),
    away_team_id    SMALLINT    NOT NULL REFERENCES pl.dim_teams(team_id),
    venue_id        SMALLINT    REFERENCES pl.dim_venues(venue_id),
    referee_id      SMALLINT    REFERENCES pl.dim_referees(referee_id),
    fpl_fixture_id  SMALLINT,
    -- Score
    fthg            SMALLINT    CHECK (fthg >= 0),
    ftag            SMALLINT    CHECK (ftag >= 0),
    ftr             CHAR(1)     CHECK (ftr IN ('H','D','A')),
    hthg            SMALLINT,
    htag            SMALLINT,
    htr             CHAR(1)     CHECK (htr IN ('H','D','A')),
    -- Statistiques traditionnelles
    home_shots      SMALLINT,  away_shots      SMALLINT,
    home_sot        SMALLINT,  away_sot        SMALLINT,
    home_fouls      SMALLINT,  away_fouls      SMALLINT,
    home_corners    SMALLINT,  away_corners    SMALLINT,
    home_yellow     SMALLINT,  away_yellow     SMALLINT,
    home_red        SMALLINT,  away_red        SMALLINT,
    -- Expected goals (FPL / sources avancées) + proxy modélisé
    home_xg         NUMERIC(5,2), away_xg        NUMERIC(5,2),
    home_xg_proxy   NUMERIC(5,2), away_xg_proxy  NUMERIC(5,2),
    -- Cotes 1X2 (pré-match)
    odds_b365_h     NUMERIC(6,2), odds_b365_d    NUMERIC(6,2), odds_b365_a   NUMERIC(6,2),
    odds_ps_h       NUMERIC(6,2), odds_ps_d      NUMERIC(6,2), odds_ps_a     NUMERIC(6,2),
    odds_avg_h      NUMERIC(6,2), odds_avg_d     NUMERIC(6,2), odds_avg_a    NUMERIC(6,2),
    odds_max_h      NUMERIC(6,2), odds_max_d     NUMERIC(6,2), odds_max_a    NUMERIC(6,2),
    odds_avg_over25 NUMERIC(6,2), odds_avg_under25 NUMERIC(6,2),
    -- Lignée
    is_played       BOOLEAN     NOT NULL DEFAULT TRUE,
    source          VARCHAR(30) NOT NULL DEFAULT 'football-data',
    record_hash     CHAR(32)    NOT NULL,                    -- md5 du contenu => upsert seulement si changé
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_fact_matches_natural UNIQUE (season_id, home_team_id, away_team_id),
    CONSTRAINT ck_fact_matches_teams CHECK (home_team_id <> away_team_id),
    CONSTRAINT ck_fact_matches_result CHECK (
        ftr IS NULL OR fthg IS NULL OR
        (ftr = 'H' AND fthg > ftag) OR (ftr = 'A' AND fthg < ftag) OR (ftr = 'D' AND fthg = ftag))
);
CREATE INDEX IF NOT EXISTS ix_fm_season_home   ON pl.fact_matches (season_id, home_team_id);
CREATE INDEX IF NOT EXISTS ix_fm_season_away   ON pl.fact_matches (season_id, away_team_id);
CREATE INDEX IF NOT EXISTS ix_fm_date          ON pl.fact_matches (date_id);
CREATE INDEX IF NOT EXISTS ix_fm_gameweek      ON pl.fact_matches (gameweek_id);
CREATE INDEX IF NOT EXISTS ix_fm_referee       ON pl.fact_matches (referee_id) WHERE referee_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS brin_fm_kickoff     ON pl.fact_matches USING BRIN (kickoff_at);

-- Grain : 1 ligne = 1 joueur × 1 match (performance sportive)
CREATE TABLE IF NOT EXISTS pl.fact_player_match_stats (
    player_id         INTEGER     NOT NULL REFERENCES pl.dim_players(player_id),
    match_id          BIGINT      NOT NULL REFERENCES pl.fact_matches(match_id) ON DELETE CASCADE,
    season_id         SMALLINT    NOT NULL REFERENCES pl.dim_seasons(season_id),
    gameweek_id       INTEGER     REFERENCES pl.dim_gameweeks(gameweek_id),
    team_id           SMALLINT    NOT NULL REFERENCES pl.dim_teams(team_id),
    opponent_team_id  SMALLINT    NOT NULL REFERENCES pl.dim_teams(team_id),
    was_home          BOOLEAN     NOT NULL,
    minutes           SMALLINT    NOT NULL CHECK (minutes BETWEEN 0 AND 130),
    starts            SMALLINT,                                -- dispo depuis 2022-23
    goals_scored      SMALLINT    NOT NULL DEFAULT 0,
    assists           SMALLINT    NOT NULL DEFAULT 0,
    clean_sheets      SMALLINT    NOT NULL DEFAULT 0,
    goals_conceded    SMALLINT    NOT NULL DEFAULT 0,
    own_goals         SMALLINT    NOT NULL DEFAULT 0,
    penalties_saved   SMALLINT    NOT NULL DEFAULT 0,
    penalties_missed  SMALLINT    NOT NULL DEFAULT 0,
    yellow_cards      SMALLINT    NOT NULL DEFAULT 0,
    red_cards         SMALLINT    NOT NULL DEFAULT 0,
    saves             SMALLINT    NOT NULL DEFAULT 0,
    expected_goals            NUMERIC(5,2),                    -- NULL avant 2022-23
    expected_assists          NUMERIC(5,2),
    expected_goal_involvements NUMERIC(5,2),
    expected_goals_conceded   NUMERIC(5,2),
    defensive_contribution    SMALLINT,                        -- règle FPL depuis 2025-26
    PRIMARY KEY (player_id, match_id)
);
CREATE INDEX IF NOT EXISTS ix_fpms_match       ON pl.fact_player_match_stats (match_id);
CREATE INDEX IF NOT EXISTS ix_fpms_season_team ON pl.fact_player_match_stats (season_id, team_id);
CREATE INDEX IF NOT EXISTS ix_fpms_gameweek    ON pl.fact_player_match_stats (gameweek_id);

-- Grain : 1 ligne = 1 joueur × 1 fixture FPL (gère les doubles gameweeks)
-- Métriques "économie du jeu" FPL : points, prix, popularité, transferts, ICT
CREATE TABLE IF NOT EXISTS pl.fact_fpl_history (
    player_id          INTEGER     NOT NULL REFERENCES pl.dim_players(player_id),
    season_id          SMALLINT    NOT NULL REFERENCES pl.dim_seasons(season_id),
    fpl_fixture_id     SMALLINT    NOT NULL,
    gameweek_id        INTEGER     NOT NULL REFERENCES pl.dim_gameweeks(gameweek_id),
    match_id           BIGINT      REFERENCES pl.fact_matches(match_id) ON DELETE SET NULL,
    team_id            SMALLINT    REFERENCES pl.dim_teams(team_id),
    opponent_team_id   SMALLINT    REFERENCES pl.dim_teams(team_id),
    was_home           BOOLEAN,
    kickoff_at         TIMESTAMPTZ,
    total_points       SMALLINT    NOT NULL,
    bonus              SMALLINT    NOT NULL DEFAULT 0,
    bps                SMALLINT,
    influence          NUMERIC(6,1),
    creativity         NUMERIC(6,1),
    threat             NUMERIC(6,1),
    ict_index          NUMERIC(6,1),
    price              NUMERIC(4,1) NOT NULL,                  -- en £m (value FPL / 10)
    selected_by        INTEGER,                                -- nb de managers
    transfers_in       INTEGER,
    transfers_out      INTEGER,
    transfers_balance  INTEGER,
    minutes            SMALLINT,
    record_hash        CHAR(32)    NOT NULL,
    loaded_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (player_id, season_id, fpl_fixture_id)
);
CREATE INDEX IF NOT EXISTS ix_ffh_gameweek       ON pl.fact_fpl_history (gameweek_id);
CREATE INDEX IF NOT EXISTS ix_ffh_season_player  ON pl.fact_fpl_history (season_id, player_id, gameweek_id);
CREATE INDEX IF NOT EXISTS ix_ffh_match          ON pl.fact_fpl_history (match_id);

-- Snapshot quotidien (saison en cours) : suivi prix / sélection / forme entre les GWs
CREATE TABLE IF NOT EXISTS pl.fact_fpl_player_snapshot (
    player_id          INTEGER     NOT NULL REFERENCES pl.dim_players(player_id),
    snapshot_date      DATE        NOT NULL,
    season_id          SMALLINT    NOT NULL REFERENCES pl.dim_seasons(season_id),
    price              NUMERIC(4,1) NOT NULL,
    selected_by_pct    NUMERIC(5,2),
    form               NUMERIC(4,1),
    points_per_game    NUMERIC(4,1),
    total_points       SMALLINT,
    transfers_in_event INTEGER,
    transfers_out_event INTEGER,
    status             CHAR(1),                                -- a/d/i/s/u/n
    chance_of_playing_next_round SMALLINT,
    news               TEXT,
    PRIMARY KEY (player_id, snapshot_date)
);
CREATE INDEX IF NOT EXISTS ix_ffps_season_date ON pl.fact_fpl_player_snapshot (season_id, snapshot_date);

-- -----------------------------------------------------------------------------
--  META : pilotage, idempotence, qualité
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS meta.etl_runs (
    run_id         BIGSERIAL   PRIMARY KEY,
    pipeline       VARCHAR(60) NOT NULL,
    mode           VARCHAR(12) NOT NULL CHECK (mode IN ('backfill','incremental','snapshot','manual')),
    season_id      SMALLINT,
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at    TIMESTAMPTZ,
    status         VARCHAR(10) NOT NULL DEFAULT 'running'
                   CHECK (status IN ('running','success','failed','skipped')),
    rows_in        INTEGER,
    rows_upserted  INTEGER,
    rows_rejected  INTEGER,
    message        TEXT
);
CREATE INDEX IF NOT EXISTS ix_etl_runs_pipeline ON meta.etl_runs (pipeline, started_at DESC);

-- Watermark par source × saison : base du chargement incrémental
CREATE TABLE IF NOT EXISTS meta.etl_watermarks (
    source            VARCHAR(40) NOT NULL,
    season_id         SMALLINT    NOT NULL,
    last_gameweek     SMALLINT,
    last_match_date   DATE,
    last_success_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    content_checksum  CHAR(32),
    PRIMARY KEY (source, season_id)
);

-- Lignes rejetées par la validation Pydantic (quarantaine)
CREATE TABLE IF NOT EXISTS meta.rejected_rows (
    id          BIGSERIAL   PRIMARY KEY,
    run_id      BIGINT      REFERENCES meta.etl_runs(run_id) ON DELETE CASCADE,
    source      VARCHAR(40) NOT NULL,
    season_id   SMALLINT,
    payload     JSONB       NOT NULL,
    errors      JSONB       NOT NULL,
    rejected_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- -----------------------------------------------------------------------------
--  SEED : saisons 2016-17 -> 2026-27 et calendrier
-- -----------------------------------------------------------------------------
INSERT INTO pl.dim_seasons (season_id, label, start_date, end_date, is_current, is_frozen)
SELECT y,
       y::text || '-' || lpad(((y + 1) % 100)::text, 2, '0'),
       make_date(y, 7, 1),
       make_date(y + 1, 6, 30),
       y = 2026,
       y < 2026
FROM generate_series(2016, 2026) AS y
ON CONFLICT (season_id) DO NOTHING;

-- 2019-20 s'est terminée fin juillet 2020 (COVID) ; 2020-21 a démarré en septembre
UPDATE pl.dim_seasons SET end_date = DATE '2020-08-15' WHERE season_id = 2019;
UPDATE pl.dim_seasons SET start_date = DATE '2020-08-16' WHERE season_id = 2020;

INSERT INTO pl.dim_dates (date_id, calendar_date, year, month, day, iso_week,
                          day_of_week, day_name, is_weekend, season_id)
SELECT to_char(d, 'YYYYMMDD')::int,
       d::date,
       extract(year FROM d)::smallint,
       extract(month FROM d)::smallint,
       extract(day FROM d)::smallint,
       extract(week FROM d)::smallint,
       extract(isodow FROM d)::smallint,
       trim(to_char(d, 'Day')),
       extract(isodow FROM d) IN (6, 7),
       (SELECT s.season_id FROM pl.dim_seasons s WHERE d::date BETWEEN s.start_date AND s.end_date)
FROM generate_series(DATE '2016-07-01', DATE '2027-06-30', INTERVAL '1 day') AS d
ON CONFLICT (date_id) DO NOTHING;
