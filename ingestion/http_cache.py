"""Rate-limited HTTP client with a plain disk cache.

Every scraper (FBref, Understat, Capology) needs the same three things:
a polite delay between requests, a cache so we don't re-fetch pages
we've already seen, and a shared user agent. This module owns those.

Cache key is SHA256(method + url + form-body), so a POST with a
different body gets its own slot.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import structlog

log = structlog.get_logger()


DEFAULT_UA = (
    "front-office-copilot/0.1 (research; contact via github.com/sankalans/fb_manager_agent)"
)


def _default_cache_dir() -> Path:
    env = os.environ.get("FOC_CACHE_DIR")
    if env:
        return Path(env)
    return Path.home() / ".cache" / "foc"


@dataclass
class CachedHTTPClient:
    """Rate-limited GET with disk cache.

    - `min_interval_seconds`: minimum wall time between two real network
      requests. Cache hits do not count.
    - `default_ttl_seconds`: how long a cached body is considered fresh.
    """

    subdir: str
    min_interval_seconds: float = 6.0
    default_ttl_seconds: int = 60 * 60 * 24
    user_agent: str = DEFAULT_UA
    base_dir: Path | None = None
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        root = self.base_dir or _default_cache_dir()
        self._dir = root / self.subdir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._last_request_at: float = 0.0
        self._client = httpx.Client(
            headers={"User-Agent": self.user_agent, "Accept-Language": "en"},
            timeout=self.timeout_seconds,
            follow_redirects=True,
        )

    def _paths(self, method: str, url: str, body: str = "") -> tuple[Path, Path]:
        key = f"{method}|{url}|{body}"
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        shard = self._dir / digest[:2]
        shard.mkdir(parents=True, exist_ok=True)
        return shard / f"{digest}.body", shard / f"{digest}.meta.json"

    def _is_fresh(self, meta_path: Path, ttl_seconds: int) -> bool:
        if not meta_path.exists():
            return False
        meta = json.loads(meta_path.read_text())
        fetched_at = datetime.fromisoformat(meta["fetched_at"])
        return datetime.now(UTC) - fetched_at < timedelta(seconds=ttl_seconds)

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        wait = self.min_interval_seconds - elapsed
        if wait > 0:
            log.debug("rate-limit sleep", seconds=round(wait, 2))
            time.sleep(wait)

    def get(self, url: str, ttl_seconds: int | None = None) -> str:
        """Return response body. Serve from cache if fresh, else fetch."""
        return self._fetch("GET", url, data=None, headers=None, ttl_seconds=ttl_seconds)

    def post_form(
        self,
        url: str,
        data: dict[str, Any],
        headers: dict[str, str] | None = None,
        ttl_seconds: int | None = None,
    ) -> str:
        """POST url-encoded form data. Cached by (url, body)."""
        return self._fetch("POST", url, data=data, headers=headers, ttl_seconds=ttl_seconds)

    def _fetch(
        self,
        method: str,
        url: str,
        data: dict[str, Any] | None,
        headers: dict[str, str] | None,
        ttl_seconds: int | None,
    ) -> str:
        ttl = ttl_seconds if ttl_seconds is not None else self.default_ttl_seconds
        body_key = json.dumps(data, sort_keys=True) if data is not None else ""
        body_path, meta_path = self._paths(method, url, body_key)

        if self._is_fresh(meta_path, ttl):
            log.debug("cache hit", url=url, method=method)
            return body_path.read_text(encoding="utf-8")

        self._throttle()
        log.info("http request", method=method, url=url)
        resp = self._client.request(method, url, data=data, headers=headers)
        self._last_request_at = time.monotonic()
        resp.raise_for_status()

        body = resp.text
        body_path.write_text(body, encoding="utf-8")
        meta_path.write_text(
            json.dumps(
                {
                    "method": method,
                    "url": url,
                    "body_key": body_key,
                    "fetched_at": datetime.now(UTC).isoformat(),
                }
            ),
            encoding="utf-8",
        )
        return body

    def close(self) -> None:
        self._client.close()
