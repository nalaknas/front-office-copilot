-- 0001_init: core schema per spec §4.
--
-- Idempotent. Safe to re-run. Every DDL uses IF NOT EXISTS. External-ID
-- uniqueness is enforced by partial unique indexes so the columns can be
-- NULL for players/clubs where a given source has no ID.
--
-- Deliberately excludes player_signals (spec §7 phase 9 stretch).

BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;


CREATE TABLE IF NOT EXISTS clubs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name TEXT NOT NULL,
    league TEXT,
    tm_id TEXT,
    companies_house_number TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS clubs_tm_id_uniq
    ON clubs (tm_id) WHERE tm_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS clubs_companies_house_uniq
    ON clubs (companies_house_number) WHERE companies_house_number IS NOT NULL;


CREATE TABLE IF NOT EXISTS players (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name TEXT NOT NULL,
    dob DATE,
    position TEXT,
    position_group TEXT,          -- GK / DEF / MID / FWD
    nationality TEXT,
    current_club_id BIGINT REFERENCES clubs (id) ON DELETE SET NULL,
    fbref_id TEXT,
    understat_id TEXT,
    tm_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS players_tm_id_uniq
    ON players (tm_id) WHERE tm_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS players_fbref_id_uniq
    ON players (fbref_id) WHERE fbref_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS players_understat_id_uniq
    ON players (understat_id) WHERE understat_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS players_current_club_idx
    ON players (current_club_id);


CREATE TABLE IF NOT EXISTS player_stats (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    player_id BIGINT NOT NULL REFERENCES players (id) ON DELETE CASCADE,
    season TEXT NOT NULL,          -- '2024-25'
    matchweek_range int4range,     -- NULL = whole season
    minutes INTEGER,
    goals_p90 NUMERIC(6, 3),
    assists_p90 NUMERIC(6, 3),
    xg_p90 NUMERIC(6, 3),
    xa_p90 NUMERIC(6, 3),
    raw_stats JSONB NOT NULL DEFAULT '{}'::jsonb,
    source TEXT NOT NULL,          -- 'fbref' / 'understat' / 'combined'
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS player_stats_player_season_idx
    ON player_stats (player_id, season);


CREATE TABLE IF NOT EXISTS contracts (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    player_id BIGINT NOT NULL REFERENCES players (id) ON DELETE CASCADE,
    club_id BIGINT NOT NULL REFERENCES clubs (id) ON DELETE CASCADE,
    start_date DATE,
    end_date DATE,
    weekly_wage_est NUMERIC(12, 2),   -- EUR
    wage_source TEXT                  -- 'capology' / 'manual' / ...
);

CREATE INDEX IF NOT EXISTS contracts_player_idx ON contracts (player_id);
CREATE INDEX IF NOT EXISTS contracts_club_idx ON contracts (club_id);


CREATE TABLE IF NOT EXISTS transfers (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    player_id BIGINT NOT NULL REFERENCES players (id) ON DELETE CASCADE,
    from_club_id BIGINT REFERENCES clubs (id) ON DELETE SET NULL,
    to_club_id BIGINT REFERENCES clubs (id) ON DELETE SET NULL,
    fee NUMERIC(14, 2),               -- EUR; NULL = free / undisclosed
    date DATE,
    contract_years INTEGER,
    tm_id TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS transfers_tm_id_uniq
    ON transfers (tm_id) WHERE tm_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS transfers_player_idx ON transfers (player_id);


CREATE TABLE IF NOT EXISTS club_finances (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    club_id BIGINT NOT NULL REFERENCES clubs (id) ON DELETE CASCADE,
    fy_end DATE NOT NULL,
    revenue NUMERIC(16, 2),
    wage_bill NUMERIC(16, 2),
    amortization NUMERIC(16, 2),
    profit_loss NUMERIC(16, 2),
    source_filing_url TEXT,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS club_finances_club_fy_uniq
    ON club_finances (club_id, fy_end);


CREATE TABLE IF NOT EXISTS watchlist (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    player_id BIGINT NOT NULL REFERENCES players (id) ON DELETE CASCADE,
    added_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    added_reason TEXT,
    thesis_fit_score NUMERIC(4, 3)   -- 0.000 - 1.000
);

CREATE UNIQUE INDEX IF NOT EXISTS watchlist_player_uniq ON watchlist (player_id);


-- FOC-13 will pick an embedding model; 1536 covers OpenAI
-- text-embedding-3-small/large-1536 and Voyage-3-lite. A later migration
-- can ALTER the column if the choice changes.
CREATE TABLE IF NOT EXISTS regulation_chunks (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_doc TEXT NOT NULL,
    article TEXT,
    section TEXT,
    content TEXT NOT NULL,
    embedding vector(1536),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS regulation_chunks_source_doc_idx
    ON regulation_chunks (source_doc);
CREATE INDEX IF NOT EXISTS regulation_chunks_content_fts_idx
    ON regulation_chunks USING gin (to_tsvector('english', content));


CREATE TABLE IF NOT EXISTS data_freshness (
    source TEXT PRIMARY KEY,
    last_run_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL,             -- 'success' / 'partial' / 'failed'
    rows_affected INTEGER,
    error TEXT
);

COMMIT;
