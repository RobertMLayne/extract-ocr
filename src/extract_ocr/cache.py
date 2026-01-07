from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .http_client import FetchResult, load_json


@dataclass(frozen=True)
class CacheEntry:
    body_path: Path
    meta_path: Path


def cache_paths(cache_dir: Path, *, key: str) -> CacheEntry:
    cache_dir.mkdir(parents=True, exist_ok=True)
    return CacheEntry(
        body_path=cache_dir / f"{key}.bin", meta_path=cache_dir / f"{key}.json"
    )


def read_cached(entry: CacheEntry) -> tuple[bytes | None, dict | None]:
    if not entry.body_path.exists() or not entry.meta_path.exists():
        return None, None
    meta = load_json(entry.meta_path)
    if meta is None:
        return None, None
    try:
        body = entry.body_path.read_bytes()
    except OSError:
        return None, None
    return body, meta


def write_cached(entry: CacheEntry, result: FetchResult) -> None:
    entry.body_path.write_bytes(result.body)
    meta = {
        "url": result.url,
        "final_url": result.final_url,
        "status_code": result.status_code,
        "headers": result.headers,
        "fetched_at": result.fetched_at,
        "etag": result.headers.get("ETag"),
        "last_modified": result.headers.get("Last-Modified"),
    }
    entry.meta_path.write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )
