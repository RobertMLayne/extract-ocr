from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Final
from urllib.parse import urlparse

from .urls import is_asset_intent_url


class ContentKind(str, Enum):
    HTML = "html"
    JSON = "json"
    XML = "xml"
    PDF = "pdf"
    TEXT = "text"
    ZIP = "zip"
    BYTES = "bytes"


_AWS_WAF_INTEGRATION_MARKERS: Final[tuple[re.Pattern[str], ...]] = (
    # Many legitimate data.uspto.gov pages include AWS WAF integration
    # (challenge script loader and cookie-domain setup). Treat these as
    # *signals* but not sufficient by themselves.
    re.compile(r"edge\.sdk\.awswaf\.com", re.IGNORECASE),
    re.compile(r"awsWafCookieDomainList", re.IGNORECASE),
    re.compile(r"challenge\.js", re.IGNORECASE),
)

_HARD_BLOCK_MARKERS: Final[tuple[re.Pattern[str], ...]] = (
    # High-confidence interstitial text markers.
    re.compile(r"Request\s+blocked", re.IGNORECASE),
    re.compile(r"You\s+have\s+been\s+blocked", re.IGNORECASE),
    re.compile(r"The\s+requested\s+URL\s+was\s+rejected", re.IGNORECASE),
)


def looks_like_html(data: bytes) -> bool:
    head = data[:2048].lstrip()
    return head.startswith(b"<") and (
        b"<html" in head.lower()
        or b"<!doctype" in head.lower()
        or b"<head" in head.lower()
    )


def is_waf_challenge(
    body: bytes,
    *,
    content_type: str | None,
    allow_integration_heuristic: bool = True,
) -> bool:
    # Only attempt expensive checks when it looks HTML-ish.
    if content_type:
        ct = content_type.split(";", 1)[0].strip().lower()
        if ct not in {"text/html", "application/xhtml+xml"} and not looks_like_html(
            body
        ):
            return False
    elif not looks_like_html(body):
        return False

    try:
        text = body[:200_000].decode("utf-8", errors="ignore")
    except UnicodeDecodeError:
        return False

    # If there are explicit block messages, treat as a challenge.
    if any(p.search(text) for p in _HARD_BLOCK_MARKERS):
        return True

    # Optional: AWS WAF integration is present on many legitimate pages.
    # When ingesting browser-saved HTML snapshots (seeds), we want to be
    # conservative and avoid skipping pages based on integration heuristics.
    if not allow_integration_heuristic:
        return False

    # Avoid false positives by only calling it a "challenge" when the HTML
    # looks like a thin interstitial (very little content/structure).
    if not any(p.search(text) for p in _AWS_WAF_INTEGRATION_MARKERS):
        return False

    # Heuristic: interstitial responses are usually minimal shells with few
    # links.
    # Legit pages generally contain a navigation/header with many anchors.
    anchor_count = len(re.findall(r"<\s*a\b", text, flags=re.IGNORECASE))
    if anchor_count >= 5:
        return False

    # If it looks like an AWS WAF page and has very few anchors, treat it as a
    # challenge response.
    return True


def sniff_kind(
    url: str,
    *,
    content_type: str | None,
    body: bytes,
) -> ContentKind:
    """Classify content conservatively.

    Rules:
    - Trust magic bytes for PDF/ZIP.
    - Treat asset-intent URLs as BYTES unless magic bytes suggest otherwise.
    - Treat HTML only when body looks like HTML and URL isn't asset-intent.
    """

    # Magic bytes.
    if body.startswith(b"%PDF-"):
        return ContentKind.PDF
    if body.startswith(b"PK\x03\x04"):
        return ContentKind.ZIP

    # Asset-intent URLs should never be treated as HTML pages.
    if is_asset_intent_url(url):
        # Some sites serve JSON from .js endpoints; handle that lightly.
        if content_type:
            ct = content_type.split(";", 1)[0].strip().lower()
            if ct in {"application/json", "text/json"}:
                return ContentKind.JSON
        return ContentKind.BYTES

    # Header hint.
    if content_type:
        ct = content_type.split(";", 1)[0].strip().lower()
        if ct in {"application/json", "text/json"}:
            return ContentKind.JSON
        if ct in {"application/xml", "text/xml"}:
            return ContentKind.XML
        if ct in {"text/plain"}:
            return ContentKind.TEXT
        if ct in {"text/html", "application/xhtml+xml"}:
            return ContentKind.HTML

    # Sniff HTML.
    if looks_like_html(body):
        return ContentKind.HTML

    # Fallback by path.
    path = urlparse(url).path.lower()
    if path.endswith(".json"):
        return ContentKind.JSON
    if path.endswith(".xml"):
        return ContentKind.XML
    if path.endswith(".txt"):
        return ContentKind.TEXT

    return ContentKind.BYTES


@dataclass(frozen=True)
class StoredArtifact:
    rel_path_posix: str
    sha256: str
    size_bytes: int
    kind: ContentKind
