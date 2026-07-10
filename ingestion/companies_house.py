"""FOC-11 importer: Companies House -> Supabase club_finances (Part A).

Scope of this pass:
  - Client to fetch each PL club's most recent 'accounts' filing.
  - Record fy_end (the accounting period end, from document metadata's
    significant_date) and a human-readable source_filing_url on
    club_finances.
  - Leave revenue and wage_bill NULL.

Why the split: nearly every football club's latest accounts is filed as
a scanned/produced PDF, not iXBRL. Machine-extracting revenue and
wage_bill therefore needs a PDF parsing pipeline (regex over statement
sections, pdfplumber). That is tracked as FOC-11b -- see architecture
doc.

Run:  uv run python -m ingestion.companies_house
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import UTC, date, datetime
from typing import Any

import psycopg
import structlog
from api.logging import configure_logging
from psycopg import Connection

from ingestion.http_cache import CachedHTTPClient

configure_logging()
log = structlog.get_logger()


SOURCE = "companies_house"
API_BASE = "https://api.company-information.service.gov.uk"


# UK-registered company covering each PL club's football operations.
# Some (e.g. Manchester United plc) are US-listed holdcos with no UK
# filings; in those cases we point at the UK subsidiary that files.
CLUB_TO_COMPANY_NUMBER = {
    "Arsenal Football Club": "00109244",
    "Aston Villa Football Club": "09409793",
    "Association Football Club Bournemouth": "06632170",
    "Brentford Football Club": "03642327",
    "Brighton and Hove Albion Football Club": "00081077",
    "Burnley Football Club": "00054222",
    "Chelsea Football Club": "01965149",
    # CPFC 2010 LIMITED files consolidated group accounts (revenue + wages
    # of the football operation); CPFC LIMITED is a downstream subsidiary.
    "Crystal Palace Football Club": "07206409",
    "Everton Football Club": "00036624",
    "Fulham Football Club": "02114486",
    "Leeds United Association Football Club": "06233875",
    "Liverpool Football Club": "00035668",
    "Manchester City Football Club": "00040946",
    "Manchester United Football Club": "00095489",
    "Newcastle United Football Club": "05981582",
    "Nottingham Forest Football Club": "01630402",
    "Sunderland Association Football Club": "00049116",
    "Tottenham Hotspur Football Club": "01706358",
    "West Ham United Football Club": "00066516",
    "Wolverhampton Wanderers Football Club": "01989823",
}


class NoAccountsFiling(Exception):
    """No accounts filing found on Companies House."""


def _api_client(key: str) -> CachedHTTPClient:
    return CachedHTTPClient(
        subdir="companies_house/api",
        min_interval_seconds=0.5,  # CH is 600 req / 5 min
        default_ttl_seconds=60 * 60 * 24 * 7,
        auth=(key, ""),
    )


def _latest_accounts(client: CachedHTTPClient, company_number: str) -> dict[str, Any]:
    url = f"{API_BASE}/company/{company_number}/filing-history?category=accounts&items_per_page=25"
    body = client.get(url)
    payload = json.loads(body)
    for item in payload.get("items", []):
        if item.get("category") == "accounts" and item.get("links", {}).get("document_metadata"):
            return dict(item)
    raise NoAccountsFiling(company_number)


def _fy_end_from_metadata(client: CachedHTTPClient, filing: dict[str, Any]) -> date | None:
    """Fetch document metadata and return the accounting period end date."""
    meta_link = filing.get("links", {}).get("document_metadata")
    if not meta_link:
        return None
    meta_body = client.get(meta_link)
    meta = json.loads(meta_body)
    # significant_date is the accounting period end when significant_date_type
    # is "made-up-date" (~always for annual accounts).
    sig = meta.get("significant_date")
    if not sig:
        return None
    try:
        return date.fromisoformat(sig[:10])
    except ValueError:
        return None


def _load_club_map(conn: Connection) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT name, id FROM clubs WHERE name = ANY(%s)",
            (list(CLUB_TO_COMPANY_NUMBER.keys()),),
        )
        return {name: cid for name, cid in cur.fetchall()}


def _upsert(
    conn: Connection,
    club_id: int,
    fy_end: date,
    source_filing_url: str,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO club_finances (club_id, fy_end, source_filing_url)
            VALUES (%s, %s, %s)
            ON CONFLICT (club_id, fy_end) DO UPDATE SET
                source_filing_url = EXCLUDED.source_filing_url,
                ingested_at = now()
            """,
            (club_id, fy_end, source_filing_url),
        )
    conn.commit()


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


def _filing_url(company_number: str, self_link: str) -> str:
    """Human-readable filing URL on Companies House 'Find and update'."""
    txn = self_link.rsplit("/", 1)[-1]
    return (
        f"https://find-and-update.company-information.service.gov.uk"
        f"/company/{company_number}/filing-history/{txn}/document"
    )


def run() -> int:
    dsn = os.environ.get("DATABASE_URL")
    key = os.environ.get("CH_API_KEY")
    if not dsn:
        log.error("DATABASE_URL not set")
        return 1
    if not key:
        log.error("CH_API_KEY not set")
        return 1

    api = _api_client(key)
    started = time.time()
    written = 0
    unresolved: list[str] = []

    with psycopg.connect(dsn) as conn:
        try:
            club_map = _load_club_map(conn)
            log.info("loaded clubs", count=len(club_map))

            for club_name, company_number in CLUB_TO_COMPANY_NUMBER.items():
                club_id = club_map.get(club_name)
                if club_id is None:
                    log.warning("club not found in db", name=club_name)
                    unresolved.append(club_name)
                    continue

                try:
                    filing = _latest_accounts(api, company_number)
                except NoAccountsFiling:
                    log.warning("no accounts filing", club=club_name, company=company_number)
                    unresolved.append(club_name)
                    continue
                except Exception as exc:
                    log.warning(
                        "filing lookup failed",
                        club=club_name,
                        error=str(exc)[:200],
                    )
                    unresolved.append(club_name)
                    continue

                try:
                    fy_end = _fy_end_from_metadata(api, filing)
                except Exception as exc:
                    log.warning(
                        "metadata fetch failed",
                        club=club_name,
                        error=str(exc)[:200],
                    )
                    fy_end = None

                if fy_end is None:
                    filing_date_str = filing.get("date")
                    fy_end = date.fromisoformat(filing_date_str) if filing_date_str else None

                if fy_end is None:
                    log.warning("no fy_end; skipping", club=club_name)
                    unresolved.append(club_name)
                    continue

                self_link = filing.get("links", {}).get("self", "")
                url = _filing_url(company_number, self_link)

                _upsert(conn, club_id=club_id, fy_end=fy_end, source_filing_url=url)
                written += 1
                log.info(
                    "club_finances upserted",
                    club=club_name,
                    fy_end=str(fy_end),
                    filing_date=filing.get("date"),
                )

            log.info(
                "companies house load complete",
                clubs_written=written,
                unresolved=unresolved,
            )
            _write_freshness(conn, "success", written)
        except Exception as exc:
            _write_freshness(conn, "failed", written, error=str(exc)[:500])
            raise
        finally:
            api.close()

    log.info("done", seconds=round(time.time() - started, 1))
    return 0


if __name__ == "__main__":
    sys.exit(run())
