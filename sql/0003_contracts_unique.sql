-- 0003_contracts_unique: natural unique key for idempotent contract upserts.
--
-- The Transfermarkt-derived importer (FOC-7) writes one contract row per
-- (player, current_club) pair. Wage-source imports (FOC-10, Capology)
-- will update the same row. A unique index gives us a valid ON CONFLICT
-- target so re-running is a no-op.

BEGIN;

CREATE UNIQUE INDEX IF NOT EXISTS contracts_player_club_uniq
    ON contracts (player_id, club_id);

COMMIT;
