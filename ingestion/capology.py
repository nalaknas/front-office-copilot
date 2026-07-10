"""FOC-10 importer: Capology salaries -> Supabase contracts.weekly_wage_est.

Capology serves per-club salary pages as regular HTML. Player rows are
embedded as JavaScript object literals inside a `<script>` tag with
`accounting.formatMoney("<annual_eur>"/52, ...)`. We regex out the
player name + annual EUR total, divide by 52 for weekly, and upsert
into `contracts` (natural key player_id + club_id, added in 0003).

Run:  uv run python -m ingestion.capology
"""

from __future__ import annotations

import html
import os
import re
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


SOURCE = "capology"
BATCH = 500

BASE_URL = "https://www.capology.com"


# Capology's URL slug -> our clubs.name. Their slugging is close to
# lowercase-hyphen-name but drops "united"/"and"/"wanderers" for a few
# clubs, so we spell them out.
CAPOLOGY_SLUG_TO_CLUB_NAME = {
    "arsenal": "Arsenal Football Club",
    "aston-villa": "Aston Villa Football Club",
    "bournemouth": "Association Football Club Bournemouth",
    "brentford": "Brentford Football Club",
    "brighton": "Brighton and Hove Albion Football Club",
    "burnley": "Burnley Football Club",
    "chelsea": "Chelsea Football Club",
    "crystal-palace": "Crystal Palace Football Club",
    "everton": "Everton Football Club",
    "fulham": "Fulham Football Club",
    "leeds": "Leeds United Association Football Club",
    "liverpool": "Liverpool Football Club",
    "manchester-city": "Manchester City Football Club",
    "manchester-united": "Manchester United Football Club",
    "newcastle": "Newcastle United Football Club",
    "nottingham-forest": "Nottingham Forest Football Club",
    "sunderland": "Sunderland Association Football Club",
    "tottenham": "Tottenham Hotspur Football Club",
    "west-ham": "West Ham United Football Club",
    "wolverhampton": "Wolverhampton Wanderers Football Club",
}


# The salary rows are formatted as:
#   "name": "<a class='firstcol' href='/player/slug/'><img ...>Player Name</a>",
#   ...
#   "weekly_gross_eur":accounting.formatMoney("<annual_eur>"/52, "€ ", 0),
_ROW_RE = re.compile(
    r'"name":\s*"<a[^>]*href=\'/player/([\w-]+)/\'[^>]*>[^<]*(?:<img[^>]*>)?([^<]+)</a>"'
    r'.*?"weekly_gross_eur":\s*accounting\.formatMoney\("(\d+)"/52',
    re.DOTALL,
)


def _season_url(slug: str, season: str) -> str:
    # '2025-26' -> '2025-2026'; already-expanded form passes through.
    season_slug = f"{season[:4]}-20{season[5:]}" if len(season) == 7 else season
    return f"{BASE_URL}/club/{slug}/salaries/{season_slug}/"


def _norm_player(name: str) -> str:
    """Same normalizer used by Understat: unescape, strip diacritics, drop punct."""
    unescaped = html.unescape(name)
    decomposed = unicodedata.normalize("NFKD", unescaped)
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    lowered = ascii_only.lower()
    cleaned = "".join(c if c.isalnum() or c.isspace() else " " for c in lowered)
    return " ".join(cleaned.split())


def _parse_rows(html_body: str) -> list[tuple[str, str, int]]:
    """Return [(cap_slug, cap_name, annual_eur)]."""
    return [(slug, name.strip(), int(annual)) for slug, name, annual in _ROW_RE.findall(html_body)]


def _load_club_map(conn: Connection) -> dict[str, int]:
    """Return {capology_slug: club_id} for the 20 current PL clubs."""
    names = list(CAPOLOGY_SLUG_TO_CLUB_NAME.values())
    with conn.cursor() as cur:
        cur.execute("SELECT name, id FROM clubs WHERE name = ANY(%s)", (names,))
        by_name = {name: cid for name, cid in cur.fetchall()}
    club_map: dict[str, int] = {}
    for slug, db_name in CAPOLOGY_SLUG_TO_CLUB_NAME.items():
        cid = by_name.get(db_name)
        if cid is None:
            log.warning("club not found", slug=slug, expected=db_name)
            continue
        club_map[slug] = cid
    return club_map


def _load_player_map(conn: Connection, club_ids: Sequence[int]) -> dict[tuple[int, str], int]:
    """Return {(club_id, normalized_name): player_id} for players in these clubs."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, name, current_club_id FROM players WHERE current_club_id = ANY(%s)",
            (list(club_ids),),
        )
        rows = cur.fetchall()
    return {(club_id, _norm_player(name)): pid for pid, name, club_id in rows}


def _upsert_wages(conn: Connection, rows: Sequence[tuple[int, int, float]]) -> int:
    """rows = [(player_id, club_id, weekly_eur)]. Returns count written."""
    if not rows:
        return 0
    total = 0
    with conn.cursor() as cur:
        for start in range(0, len(rows), BATCH):
            chunk = rows[start : start + BATCH]
            placeholder = "(%s,%s,%s,%s)"
            values_sql = ",".join([placeholder] * len(chunk))
            stmt = (
                "INSERT INTO contracts (player_id, club_id, weekly_wage_est, wage_source) "
                f"VALUES {values_sql} "
                "ON CONFLICT (player_id, club_id) DO UPDATE "
                "SET weekly_wage_est = EXCLUDED.weekly_wage_est, "
                "wage_source = EXCLUDED.wage_source"
            )
            flat: list[Any] = []
            for pid, cid, weekly in chunk:
                flat.extend([pid, cid, weekly, SOURCE])
            cur.execute(stmt, flat)
            total += len(chunk)
    conn.commit()
    return total


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

    client = CachedHTTPClient(subdir="capology", min_interval_seconds=8.0)
    started = time.time()

    with psycopg.connect(dsn) as conn:
        try:
            club_map = _load_club_map(conn)
            player_map = _load_player_map(conn, list(club_map.values()))
            log.info(
                "loaded lookups",
                clubs=len(club_map),
                players_in_pl_clubs=len(player_map),
            )

            wage_rows: list[tuple[int, int, float]] = []
            unmapped: list[dict[str, Any]] = []
            per_club: dict[str, dict[str, int]] = {}

            for slug, club_id in club_map.items():
                url = _season_url(slug, season)
                try:
                    body = client.get(url)
                except Exception as exc:
                    log.warning("club fetch failed", club=slug, error=str(exc)[:200])
                    per_club[slug] = {"scraped": 0, "matched": 0}
                    continue
                parsed = _parse_rows(body)
                matched = 0
                for cap_slug, cap_name, annual in parsed:
                    weekly = round(annual / 52, 2)
                    key = (club_id, _norm_player(cap_name))
                    pid = player_map.get(key)
                    if pid is None:
                        unmapped.append({"club": slug, "cap_slug": cap_slug, "name": cap_name})
                        continue
                    wage_rows.append((pid, club_id, weekly))
                    matched += 1
                per_club[slug] = {"scraped": len(parsed), "matched": matched}
                log.info(
                    "club scraped",
                    club=slug,
                    scraped=len(parsed),
                    matched=matched,
                )

            written = _upsert_wages(conn, wage_rows)

            log.info(
                "capology load complete",
                total_scraped=sum(c["scraped"] for c in per_club.values()),
                total_matched=sum(c["matched"] for c in per_club.values()),
                contracts_written=written,
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
