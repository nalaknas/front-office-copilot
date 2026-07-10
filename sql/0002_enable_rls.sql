-- 0002_enable_rls: lock down every public table.
--
-- Supabase exposes public tables to anon/authenticated roles via
-- PostgREST. With no policies, "ENABLE ROW LEVEL SECURITY" denies
-- those roles by default. Our FastAPI service connects via the direct
-- Postgres URL (the `postgres` superuser role bypasses RLS), so this
-- change is invisible to us but closes the exposure to anyone holding
-- the anon key.
--
-- Idempotent: re-enabling RLS on a table that already has it is a
-- no-op.

BEGIN;

ALTER TABLE public.clubs             ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.players           ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.player_stats      ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.contracts         ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.transfers         ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.club_finances     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.watchlist         ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.regulation_chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.data_freshness    ENABLE ROW LEVEL SECURITY;

COMMIT;
