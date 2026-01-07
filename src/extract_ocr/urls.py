from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import ParseResult, urlparse, urlunparse

_TRACKING_QUERY_EXACT = {"agt=index"}


def normalize_url(raw_url: str) -> str:
    """Normalize a URL for de-duplication.

    - Lowercases scheme + hostname.
    - Strips fragments.
    - Drops trivial tracking query params known to create duplicates.
    """

    parsed: ParseResult = urlparse(raw_url)
    scheme = (parsed.scheme or "").lower()
    netloc = (parsed.netloc or "").lower()

    query = parsed.query
    if query.strip().lower() in _TRACKING_QUERY_EXACT:
        query = ""

    parsed = parsed._replace(
        scheme=scheme,
        netloc=netloc,
        fragment="",
        query=query,
    )
    return urlunparse(parsed)


_ASSET_EXTS = {
    ".css",
    ".js",
    ".mjs",
    ".map",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".eot",
    ".pdf",
    ".zip",
    ".gz",
    ".tgz",
}


def is_asset_intent_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in _ASSET_EXTS)


def safe_filename_piece(text: str, *, max_len: int = 80) -> str:
    text = text.strip()
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    if not text:
        return "untitled"
    return text[:max_len]


@dataclass(frozen=True)
class UrlScope:
    allow_host_suffixes: tuple[str, ...]
    follow_offsite: bool

    def is_allowed(self, url: str) -> bool:
        if self.follow_offsite:
            return True
        host = (urlparse(url).hostname or "").lower()
        if not host:
            return False
        for suffix in self.allow_host_suffixes:
            suffix = suffix.lower().lstrip(".")
            if host == suffix or host.endswith("." + suffix):
                return True
        return False
