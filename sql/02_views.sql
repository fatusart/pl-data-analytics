-- =============================================================================
--  Vues analytiques (couche sémantique consommée par le dashboard / Power BI)
-- =============================================================================
SET search_path TO pl, public;

-- Match "dépivoté" : 1 ligne par équipe et par match (perspective équipe).
-- C'est la base de tous les calculs de forme, classements roulants, splits dom/ext.
CREATE OR REPLACE VIEW pl.v_team_match AS
SELECT m.match_id, m.season_id, m.gameweek_id, m.date_id, m.kickoff_at,
       m.home_team_id AS team_id, m.away_team_id AS opponent_id, TRUE AS is_home,
       m.fthg AS goals_for, m.ftag AS goals_against,
       m.home_shots AS shots_for, m.away_shots AS shots_against,
       m.home_sot AS sot_for, m.away_sot AS sot_against,
       m.home_corners AS corners_for, m.away_corners AS corners_against,
       m.home_fouls AS fouls, m.home_yellow AS yellows, m.home_red AS reds,
       COALESCE(m.home_xg, m.home_xg_proxy) AS xg_for,
       COALESCE(m.away_xg, m.away_xg_proxy) AS xg_against,
       CASE m.ftr WHEN 'H' THEN 3 WHEN 'D' THEN 1 ELSE 0 END AS points,
       CASE m.ftr WHEN 'H' THEN 'W' WHEN 'D' THEN 'D' ELSE 'L' END AS result
FROM pl.fact_matches m WHERE m.is_played
UNION ALL
SELECT m.match_id, m.season_id, m.gameweek_id, m.date_id, m.kickoff_at,
       m.away_team_id, m.home_team_id, FALSE,
       m.ftag, m.fthg,
       m.away_shots, m.home_shots,
       m.away_sot, m.home_sot,
       m.away_corners, m.home_corners,
       m.away_fouls, m.away_yellow, m.away_red,
       COALESCE(m.away_xg, m.away_xg_proxy),
       COALESCE(m.home_xg, m.home_xg_proxy),
       CASE m.ftr WHEN 'A' THEN 3 WHEN 'D' THEN 1 ELSE 0 END,
       CASE m.ftr WHEN 'A' THEN 'W' WHEN 'D' THEN 'D' ELSE 'L' END
FROM pl.fact_matches m WHERE m.is_played;

-- Classement final (ou courant) par saison, avec critères officiels PL :
-- points, différence de buts, buts marqués.
DROP MATERIALIZED VIEW IF EXISTS pl.mv_league_table;
CREATE MATERIALIZED VIEW pl.mv_league_table AS
SELECT tm.season_id,
       tm.team_id,
       t.team_name,
       t.short_name,
       COUNT(*)                                  AS played,
       SUM((tm.result = 'W')::int)               AS won,
       SUM((tm.result = 'D')::int)               AS drawn,
       SUM((tm.result = 'L')::int)               AS lost,
       SUM(tm.goals_for)                         AS goals_for,
       SUM(tm.goals_against)                     AS goals_against,
       SUM(tm.goals_for - tm.goals_against)      AS goal_diff,
       SUM(tm.points)                            AS points,
       ROUND(SUM(tm.xg_for)::numeric, 1)         AS xg_for,
       ROUND(SUM(tm.xg_against)::numeric, 1)     AS xg_against,
       RANK() OVER (PARTITION BY tm.season_id
                    ORDER BY SUM(tm.points) DESC,
                             SUM(tm.goals_for - tm.goals_against) DESC,
                             SUM(tm.goals_for) DESC) AS position
FROM pl.v_team_match tm
JOIN pl.dim_teams t USING (team_id)
GROUP BY tm.season_id, tm.team_id, t.team_name, t.short_name;
CREATE UNIQUE INDEX IF NOT EXISTS ux_mv_league_table ON pl.mv_league_table (season_id, team_id);

-- Profil arbitre : sévérité et biais domicile
DROP MATERIALIZED VIEW IF EXISTS pl.mv_referee_profile;
CREATE MATERIALIZED VIEW pl.mv_referee_profile AS
SELECT r.referee_id, r.referee_name, m.season_id,
       COUNT(*)                                                    AS matches,
       ROUND(AVG(m.home_yellow + m.away_yellow), 2)                AS yellows_per_match,
       ROUND(AVG(m.home_red + m.away_red), 3)                      AS reds_per_match,
       ROUND(AVG(m.home_fouls + m.away_fouls), 2)                  AS fouls_per_match,
       ROUND(AVG((m.ftr = 'H')::int), 3)                           AS home_win_rate,
       ROUND(AVG(m.away_yellow - m.home_yellow), 2)                AS away_minus_home_yellows
FROM pl.fact_matches m
JOIN pl.dim_referees r USING (referee_id)
WHERE m.is_played
GROUP BY r.referee_id, r.referee_name, m.season_id;
CREATE UNIQUE INDEX IF NOT EXISTS ux_mv_referee_profile ON pl.mv_referee_profile (referee_id, season_id);

-- FPL agrégé au grain joueur × gameweek (fusionne les doubles GW)
DROP VIEW IF EXISTS pl.v_fpl_player_gameweek;
CREATE VIEW pl.v_fpl_player_gameweek AS
SELECT h.season_id, h.player_id, g.gw_number, h.gameweek_id,
       p.web_name, ps.position, t.team_name AS team, t.short_name AS team_short,
       COUNT(*)                     AS fixtures,
       SUM(h.total_points)          AS points,
       SUM(h.minutes)               AS minutes,
       MAX(h.price)                 AS price,
       MAX(h.selected_by)           AS selected_by,
       SUM(h.transfers_balance)     AS transfers_balance,
       SUM(h.ict_index)             AS ict_index,
       SUM(s.expected_goal_involvements) AS xgi
FROM pl.fact_fpl_history h
JOIN pl.dim_gameweeks g     ON g.gameweek_id = h.gameweek_id
JOIN pl.dim_players p       ON p.player_id   = h.player_id
LEFT JOIN pl.dim_player_season ps ON ps.player_id = h.player_id AND ps.season_id = h.season_id
LEFT JOIN pl.dim_teams t    ON t.team_id     = h.team_id
LEFT JOIN pl.fact_player_match_stats s
       ON s.player_id = h.player_id AND s.match_id = h.match_id
GROUP BY h.season_id, h.player_id, g.gw_number, h.gameweek_id, p.web_name, ps.position,
         t.team_name, t.short_name;
