from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import requests
from requests import exceptions as req_exc

from .urls import normalize_url

TRANSIENT_HTTP_STATUSES = {429, 500, 502, 503, 504}


def _retry_after_seconds(headers: dict[str, str]) -> float | None:
    retry_after = headers.get("Retry-After")
    if not retry_after:
        return None
    try:
        return float(retry_after)
    except ValueError:
        return None


@dataclass(frozen=True)
class FetchResult:
    url: str
    final_url: str
    status_code: int
    headers: dict[str, str]
    fetched_at: float
    body: bytes
    from_cache: bool


class HttpClient:
    def __init__(
        self,
        session: requests.Session,
        *,
        timeout_s: int = 45,
        max_retries: int = 4,
        backoff_base_s: float = 1.0,
    ) -> None:
        self._session = session
        self._timeout_s = timeout_s
        self._max_retries = max_retries
        self._backoff_base_s = backoff_base_s

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> FetchResult:
        normalized = normalize_url(url)
        last_error: Exception | None = None

        for attempt in range(self._max_retries + 1):
            try:
                resp = self._session.get(
                    normalized, timeout=self._timeout_s, headers=headers
                )

                if (
                    resp.status_code in TRANSIENT_HTTP_STATUSES
                    and attempt < self._max_retries
                ):
                    retry_after = _retry_after_seconds(dict(resp.headers))
                    wait_s = (
                        retry_after
                        if retry_after is not None
                        else self._backoff_base_s * (2**attempt)
                    )
                    time.sleep(wait_s)
                    continue

                # Callers may want to inspect bodies (e.g., WAF pages).
                return FetchResult(
                    url=normalized,
                    final_url=str(resp.url),
                    status_code=int(resp.status_code),
                    headers={k: str(v) for k, v in resp.headers.items()},
                    fetched_at=time.time(),
                    body=resp.content,
                    from_cache=False,
                )
            except req_exc.RequestException as e:
                last_error = e
                if attempt >= self._max_retries:
                    break
                time.sleep(self._backoff_base_s * (2**attempt))

        raise RuntimeError(f"Failed to fetch {normalized}: {last_error}")


def load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
