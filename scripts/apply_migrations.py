"""Apply /sql/*.sql migrations in filename order.

Every SQL file is expected to be idempotent (CREATE ... IF NOT EXISTS,
etc.), so running twice is a no-op. That's the FOC-6 AC.

Usage:
    DATABASE_URL=postgresql://foc:foc@localhost:5432/foc uv run python -m scripts.apply_migrations
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"


def main() -> int:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 1

    files = sorted(SQL_DIR.glob("*.sql"))
    if not files:
        print(f"no .sql files found in {SQL_DIR}", file=sys.stderr)
        return 1

    with psycopg.connect(dsn) as conn:
        for path in files:
            print(f"applying {path.name}")
            with conn.cursor() as cur:
                cur.execute(path.read_text())
            conn.commit()

    print(f"applied {len(files)} migration(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
