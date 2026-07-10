-- 0004_player_stats_uniq: natural key for season-total upserts.
--
-- FBref (FOC-8), Understat (FOC-9), etc. each write one row per
-- (player, season, source) when reporting season totals
-- (matchweek_range IS NULL). This partial unique index gives us a
-- valid ON CONFLICT target so re-scrapes are idempotent.
-- Matchweek-scoped rows remain unconstrained.

BEGIN;

CREATE UNIQUE INDEX IF NOT EXISTS player_stats_season_uniq
    ON player_stats (player_id, season, source)
    WHERE matchweek_range IS NULL;

COMMIT;
