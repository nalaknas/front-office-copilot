"""FOC-9 importer: Understat player stats -> Supabase player_stats.

Understat exposes an undocumented but stable JSON endpoint,
POST /main/getPlayersStats/ with form data {league, season}, which
returns every player who has touched the pitch that season with
minutes, goals, assists, xG, xA, shots, key passes and derived metrics
(npxG, xGChain, xGBuildup).

We match those rows to our `players` table by (current club, name) and
upsert per-90 numbers into `player_stats` with source='understat'.
Any Understat rows we cannot map are logged, not dropped -- per the
FOC-9 acceptance criteria.

Run:  uv run python -m ingestion.understat
"""

from __future__ import annotations

import html
import json
import os
import sys
import time
import unicodedata
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import psycopg
import structlog
from api.logging import configure_logging
from psycopg import Connection

from ingestion.http_cache import CachedHTTPClient

configure_logging()
log = structlog.get_logger()


SOURCE = "understat"
LEAGUE_CODE = "EPL"
BATCH = 500

BASE_URL = "https://understat.com"
STATS_URL = f"{BASE_URL}/main/getPlayersStats/"


# Understat team_title -> our clubs.name for the 2025-26 Premier League.
# Understat drops "FC" / "United" / other tail words on some names, so a
# generic normalizer isn't enough. This is the smallest thing that works.
UNDERSTAT_TO_CLUB_NAME = {
    "Arsenal": "Arsenal Football Club",
    "Aston Villa": "Aston Villa Football Club",
    "Bournemouth": "Association Football Club Bournemouth",
    "Brentford": "Brentford Football Club",
    "Brighton": "Brighton and Hove Albion Football Club",
    "Burnley": "Burnley Football Club",
    "Chelsea": "Chelsea Football Club",
    "Crystal Palace": "Crystal Palace Football Club",
    "Everton": "Everton Football Club",
    "Fulham": "Fulham Football Club",
    "Leeds": "Leeds United Association Football Club",
    "Liverpool": "Liverpool Football Club",
    "Manchester City": "Manchester City Football Club",
    "Manchester United": "Manchester United Football Club",
    "Newcastle United": "Newcastle United Football Club",
    "Nottingham Forest": "Nottingham Forest Football Club",
    "Sunderland": "Sunderland Association Football Club",
    "Tottenham": "Tottenham Hotspur Football Club",
    "West Ham": "West Ham United Football Club",
    "Wolverhampton Wanderers": "Wolverhampton Wanderers Football Club",
}


def _season_start_year(season: str) -> int:
    """'2025-26' -> 2025. Understat uses the start year as its season id."""
    return int(season.split("-")[0])


def _norm_player(name: str) -> str:
    """Unescape HTML entities, lowercase, strip diacritics, drop punctuation."""
    unescaped = html.unescape(name)
    decomposed = unicodedata.normalize("NFKD", unescaped)
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    lowered = ascii_only.lower()
    cleaned = "".join(c if c.isalnum() or c.isspace() else " " for c in lowered)
    return " ".join(cleaned.split())


def _current_team(team_title: str) -> str:
    """Understat encodes mid-season moves as 'From,To'. Take the last."""
    return team_title.split(",")[-1].strip()


def _load_club_map(conn: Connection) -> dict[str, int]:
    """Return {understat_team_name: club_id} for the 20 current PL clubs."""
    names = list(UNDERSTAT_TO_CLUB_NAME.values())
    with conn.cursor() as cur:
        cur.execute("SELECT name, id FROM clubs WHERE name = ANY(%s)", (names,))
        by_name = {name: cid for name, cid in cur.fetchall()}
    club_map: dict[str, int] = {}
    for u_name, db_name in UNDERSTAT_TO_CLUB_NAME.items():
        cid = by_name.get(db_name)
        if cid is None:
            log.warning("club not found", understat=u_name, expected=db_name)
            continue
        club_map[u_name] = cid
    return club_map


def _load_player_map(conn: Connection, club_ids: Sequence[int]) -> dict[tuple[int, str], int]:
    """Return {(club_id, normalized_name): player_id} for players in these clubs."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, name, current_club_id FROM players WHERE current_club_id = ANY(%s)",
            (list(club_ids),),
        )
        rows = cur.fetchall()
    out: dict[tuple[int, str], int] = {}
    for pid, name, club_id in rows:
        out[(club_id, _norm_player(name))] = pid
    return out


def _fetch_players(client: CachedHTTPClient, season_year: int) -> list[dict[str, Any]]:
    body = client.post_form(
        STATS_URL,
        data={"league": LEAGUE_CODE, "season": str(season_year)},
        headers={
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{BASE_URL}/league/{LEAGUE_CODE}/{season_year}",
        },
    )
    payload = json.loads(body)
    if not payload.get("success"):
        raise RuntimeError("Understat response missing success flag")
    players = payload.get("players", [])
    assert isinstance(players, list)
    return players


def _per90(total: float, minutes: int) -> float | None:
    if minutes <= 0:
        return None
    return round((total / minutes) * 90, 3)


def _upsert_stats(
    conn: Connection,
    rows: Sequence[tuple[int, int, dict[str, Any]]],
    season: str,
) -> int:
    """rows = [(player_id, minutes, raw)]. Returns count written."""
    if not rows:
        return 0
    to_insert: list[Sequence[Any]] = []
    for player_id, minutes, raw in rows:
        goals = int(raw.get("goals", 0))
        assists = int(raw.get("assists", 0))
        xg = float(raw.get("xG", 0.0))
        xa = float(raw.get("xA", 0.0))
        to_insert.append(
            (
                player_id,
                season,
                minutes,
                _per90(goals, minutes),
                _per90(assists, minutes),
                _per90(xg, minutes),
                _per90(xa, minutes),
                json.dumps(raw),
                SOURCE,
            )
        )
    total = 0
    with conn.cursor() as cur:
        for start in range(0, len(to_insert), BATCH):
            chunk = to_insert[start : start + BATCH]
            placeholder = "(" + ",".join(["%s"] * 9) + ")"
            values_sql = ",".join([placeholder] * len(chunk))
            stmt = (
                "INSERT INTO player_stats "
                "(player_id, season, minutes, goals_p90, assists_p90, "
                "xg_p90, xa_p90, raw_stats, source) "
                f"VALUES {values_sql} "
                "ON CONFLICT (player_id, season, source) "
                "WHERE matchweek_range IS NULL "
                "DO UPDATE SET minutes = EXCLUDED.minutes, "
                "goals_p90 = EXCLUDED.goals_p90, "
                "assists_p90 = EXCLUDED.assists_p90, "
                "xg_p90 = EXCLUDED.xg_p90, "
                "xa_p90 = EXCLUDED.xa_p90, "
                "raw_stats = EXCLUDED.raw_stats, "
                "ingested_at = now()"
            )
            flat = [v for row in chunk for v in row]
            cur.execute(stmt, flat)
            total += len(chunk)
    conn.commit()
    return total


def _backfill_understat_ids(conn: Connection, matched: Sequence[tuple[int, str]]) -> int:
    """matched = [(player_id, understat_id)]. Only sets when null."""
    if not matched:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            "UPDATE players SET understat_id = %s WHERE id = %s AND understat_id IS NULL",
            [(u_id, p_id) for p_id, u_id in matched],
        )
        n = cur.rowcount
    conn.commit()
    return n if n is not None else 0


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


def run(season: str = "2025-26") -> int:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        log.error("DATABASE_URL not set")
        return 1

    season_year = _season_start_year(season)
    client = CachedHTTPClient(subdir="understat", min_interval_seconds=2.0)
    started = time.time()

    with psycopg.connect(dsn) as conn:
        try:
            club_map = _load_club_map(conn)
            player_map = _load_player_map(conn, list(club_map.values()))
            log.info(
                "loaded lookups",
                clubs=len(club_map),
                players_in_clubs=len(player_map),
            )

            players = _fetch_players(client, season_year)
            log.info("understat rows", count=len(players))

            stats_rows: list[tuple[int, int, dict[str, Any]]] = []
            id_updates: list[tuple[int, str]] = []
            unmapped: list[dict[str, Any]] = []

            for p in players:
                team = _current_team(str(p["team_title"]))
                club_id = club_map.get(team)
                minutes = int(p.get("time", 0))
                if club_id is None:
                    unmapped.append({"team": team, "name": p.get("player_name")})
                    continue
                key = (club_id, _norm_player(str(p["player_name"])))
                player_id = player_map.get(key)
                if player_id is None:
                    unmapped.append(
                        {"team": team, "name": p.get("player_name"), "reason": "no player match"}
                    )
                    continue
                if minutes <= 0:
                    continue
                stats_rows.append((player_id, minutes, p))
                id_updates.append((player_id, str(p["id"])))

            written = _upsert_stats(conn, stats_rows, season)
            backfilled = _backfill_understat_ids(conn, id_updates)

            log.info(
                "understat load complete",
                stats_written=written,
                understat_ids_backfilled=backfilled,
                unmapped=len(unmapped),
            )
            if unmapped:
                log.info("unmapped sample", first=unmapped[:5])

            _write_freshness(conn, "success", written)
        except Exception as exc:
            _write_freshness(conn, "failed", 0, error=str(exc)[:500])
            raise
        finally:
            client.close()

    log.info("done", seconds=round(time.time() - started, 1))
    return 0


if __name__ == "__main__":
    sys.exit(run())
