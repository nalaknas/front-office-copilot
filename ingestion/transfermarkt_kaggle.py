"""FOC-7 importer: davidcariboo/player-scores (Kaggle) -> Supabase.

Populates clubs, players, transfers, contracts for the top-5 European
leagues. Idempotent by design -- every write is an ON CONFLICT ... DO
UPDATE keyed on a natural unique index. A row in data_freshness records
each run.

Run:  uv run python -m ingestion.transfermarkt_kaggle
"""

from __future__ import annotations

import math
import os
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import kagglehub
import pandas as pd
import psycopg
import structlog
from api.logging import configure_logging
from psycopg import Connection

configure_logging()
log = structlog.get_logger()

# Transfermarkt competition IDs for the top-5 leagues.
TOP5_LEAGUE_CODES = ("GB1", "ES1", "L1", "IT1", "FR1")

# Human-readable name per code (fallback if competitions.csv is missing).
LEAGUE_NAME = {
    "GB1": "Premier League",
    "ES1": "LaLiga",
    "L1": "Bundesliga",
    "IT1": "Serie A",
    "FR1": "Ligue 1",
}

# TM "position" -> our four-way position_group.
POSITION_GROUP = {
    "Goalkeeper": "GK",
    "Defender": "DEF",
    "Midfield": "MID",
    "Attack": "FWD",
}

DATASET = "davidcariboo/player-scores"
SOURCE = "transfermarkt_kaggle"
BATCH = 500


def _parse_date(value: Any) -> Any:
    """Return a `date` or None. TM dates arrive as 'YYYY-MM-DD HH:MM:SS'."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    try:
        return pd.to_datetime(value).date()
    except (ValueError, TypeError):
        return None


def _nan_to_none(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def _upsert_batch(
    conn: Connection,
    table: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    conflict_target: str,
    update_columns: Sequence[str],
) -> int:
    """Batched multi-VALUES upsert. Returns the number of rows sent."""
    if not rows:
        return 0
    total = 0
    with conn.cursor() as cur:
        for start in range(0, len(rows), BATCH):
            chunk = rows[start : start + BATCH]
            placeholder = "(" + ",".join(["%s"] * len(columns)) + ")"
            values_sql = ",".join([placeholder] * len(chunk))
            set_clause = ",".join(f"{c} = EXCLUDED.{c}" for c in update_columns)
            stmt = (
                f"INSERT INTO {table} ({','.join(columns)}) "
                f"VALUES {values_sql} "
                f"ON CONFLICT {conflict_target} DO UPDATE SET {set_clause}"
            )
            flat: list[Any] = [v for row in chunk for v in row]
            cur.execute(stmt, flat)
            total += len(chunk)
    conn.commit()
    return total


def _load_clubs(conn: Connection, base: Path) -> dict[str, int]:
    """Upsert clubs; return {tm_id: db_id} for top-5 clubs."""
    df = pd.read_csv(base / "clubs.csv")
    df = df[df["domestic_competition_id"].isin(TOP5_LEAGUE_CODES)].copy()
    rows = [
        (
            row["name"],
            LEAGUE_NAME[row["domestic_competition_id"]],
            str(int(row["club_id"])),
        )
        for _, row in df.iterrows()
    ]
    written = _upsert_batch(
        conn,
        "clubs",
        columns=("name", "league", "tm_id"),
        rows=rows,
        conflict_target="(tm_id) WHERE tm_id IS NOT NULL",
        update_columns=("name", "league"),
    )
    log.info("clubs upserted", count=written)

    tm_ids = [str(int(cid)) for cid in df["club_id"].tolist()]
    with conn.cursor() as cur:
        cur.execute("SELECT tm_id, id FROM clubs WHERE tm_id = ANY(%s)", (tm_ids,))
        return {tm_id: db_id for tm_id, db_id in cur.fetchall()}


def _load_players(conn: Connection, base: Path, club_map: dict[str, int]) -> dict[str, int]:
    """Upsert players in top-5 clubs; return {tm_id: db_id}."""
    df = pd.read_csv(base / "players.csv")
    df = df[df["current_club_domestic_competition_id"].isin(TOP5_LEAGUE_CODES)].copy()

    rows: list[tuple[Any, ...]] = []
    for _, row in df.iterrows():
        club_tm = str(int(row["current_club_id"])) if pd.notna(row["current_club_id"]) else None
        club_db = club_map.get(club_tm) if club_tm else None
        rows.append(
            (
                str(row["name"]),
                _parse_date(row.get("date_of_birth")),
                _nan_to_none(row.get("sub_position")),
                POSITION_GROUP.get(str(row.get("position")).strip(), None),
                _nan_to_none(row.get("country_of_citizenship")),
                club_db,
                str(int(row["player_id"])),
            )
        )
    written = _upsert_batch(
        conn,
        "players",
        columns=(
            "name",
            "dob",
            "position",
            "position_group",
            "nationality",
            "current_club_id",
            "tm_id",
        ),
        rows=rows,
        conflict_target="(tm_id) WHERE tm_id IS NOT NULL",
        update_columns=(
            "name",
            "dob",
            "position",
            "position_group",
            "nationality",
            "current_club_id",
        ),
    )
    log.info("players upserted", count=written)

    tm_ids = [str(int(pid)) for pid in df["player_id"].tolist()]
    player_map: dict[str, int] = {}
    with conn.cursor() as cur:
        # ANY(%s) with large lists is fine but chunk to keep prepared-statement size reasonable.
        for start in range(0, len(tm_ids), 5000):
            chunk = tm_ids[start : start + 5000]
            cur.execute("SELECT tm_id, id FROM players WHERE tm_id = ANY(%s)", (chunk,))
            for tm_id, db_id in cur.fetchall():
                player_map[tm_id] = db_id
    return player_map


def _load_transfers(
    conn: Connection,
    base: Path,
    player_map: dict[str, int],
    club_map: dict[str, int],
) -> None:
    df = pd.read_csv(base / "transfers.csv")
    # Keep only transfers involving a player we ingested.
    df = df[df["player_id"].astype(str).isin(player_map.keys())].copy()

    rows: list[tuple[Any, ...]] = []
    for _, row in df.iterrows():
        player_db = player_map.get(str(int(row["player_id"])))
        if player_db is None:
            continue
        from_tm = str(int(row["from_club_id"])) if pd.notna(row["from_club_id"]) else None
        to_tm = str(int(row["to_club_id"])) if pd.notna(row["to_club_id"]) else None
        from_db = club_map.get(from_tm) if from_tm else None
        to_db = club_map.get(to_tm) if to_tm else None
        date = _parse_date(row.get("transfer_date"))
        # Synthetic natural key: player-date-to_club uniquely identifies a transfer row.
        synth_tm = f"{int(row['player_id'])}-{date}-{to_tm or 'na'}"
        fee = _nan_to_none(row.get("transfer_fee"))
        rows.append((player_db, from_db, to_db, fee, date, synth_tm))

    written = _upsert_batch(
        conn,
        "transfers",
        columns=("player_id", "from_club_id", "to_club_id", "fee", "date", "tm_id"),
        rows=rows,
        conflict_target="(tm_id) WHERE tm_id IS NOT NULL",
        update_columns=("player_id", "from_club_id", "to_club_id", "fee", "date"),
    )
    log.info("transfers upserted", count=written)


def _load_contracts(
    conn: Connection, base: Path, player_map: dict[str, int], club_map: dict[str, int]
) -> None:
    df = pd.read_csv(base / "players.csv")
    df = df[df["current_club_domestic_competition_id"].isin(TOP5_LEAGUE_CODES)].copy()

    rows: list[tuple[Any, ...]] = []
    for _, row in df.iterrows():
        exp = _parse_date(row.get("contract_expiration_date"))
        if exp is None:
            continue
        player_db = player_map.get(str(int(row["player_id"])))
        club_tm = str(int(row["current_club_id"])) if pd.notna(row["current_club_id"]) else None
        club_db = club_map.get(club_tm) if club_tm else None
        if player_db is None or club_db is None:
            continue
        rows.append((player_db, club_db, exp))

    written = _upsert_batch(
        conn,
        "contracts",
        columns=("player_id", "club_id", "end_date"),
        rows=rows,
        conflict_target="(player_id, club_id)",
        update_columns=("end_date",),
    )
    log.info("contracts upserted", count=written)


def _write_freshness(
    conn: Connection, status: str, rows_affected: int, error: str | None = None
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO data_freshness (source, last_run_at, status, rows_affected, error)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (source) DO UPDATE
            SET last_run_at = EXCLUDED.last_run_at,
                status = EXCLUDED.status,
                rows_affected = EXCLUDED.rows_affected,
                error = EXCLUDED.error
            """,
            (SOURCE, datetime.now(UTC), status, rows_affected, error),
        )
    conn.commit()


def run() -> int:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        log.error("DATABASE_URL not set")
        return 1

    log.info("downloading dataset", dataset=DATASET)
    base = Path(kagglehub.dataset_download(DATASET))
    log.info("dataset ready", path=str(base))

    started = time.time()
    with psycopg.connect(dsn) as conn:
        try:
            club_map = _load_clubs(conn, base)
            player_map = _load_players(conn, base, club_map)
            _load_transfers(conn, base, player_map, club_map)
            _load_contracts(conn, base, player_map, club_map)
            total = len(club_map) + len(player_map)
            _write_freshness(conn, "success", total)
        except Exception as exc:
            _write_freshness(conn, "failed", 0, error=str(exc)[:500])
            raise

    log.info("done", seconds=round(time.time() - started, 1))
    return 0


if __name__ == "__main__":
    sys.exit(run())
