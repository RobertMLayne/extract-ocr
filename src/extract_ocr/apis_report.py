from __future__ import annotations

import hashlib
import json
import re
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse, urlunparse

_USPTO_APIS_BASE = "https://data.uspto.gov"

# Capture both absolute and relative endpoints. We only emit endpoints under
# https://data.uspto.gov/apis/...
_ABS_APIS_RE = re.compile(
    r"https?://data\.uspto\.gov/apis/[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+",
    re.IGNORECASE,
)
_REL_APIS_RE = re.compile(
    r"(?<![A-Za-z0-9_])(/apis/[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+)",
    re.IGNORECASE,
)

_INVALID_FILENAME_CHARS = re.compile(r"[<>:\"/\\|?*\x00-\x1F]")


@dataclass(frozen=True)
class ApiEndpointFinding:
    endpoint: str
    source_url: str


def _safe_filename_component(text: str) -> str:
    cleaned = _INVALID_FILENAME_CHARS.sub("-", (text or "").strip())
    cleaned = cleaned.strip(". ")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        cleaned = "page"
    return cleaned[:150]


def _guess_title_from_url(url: str) -> str:
    try:
        parsed = urlparse(url)
    except ValueError:
        return "response"
    path = (parsed.path or "/").rstrip("/")
    last = path.split("/")[-1] if path else "response"
    if not last:
        last = "response"
    return last


def _fallback_resp_md_relpath(endpoint_url: str) -> str:
    """Best-effort deterministic path for pages/<stem>.resp.md.

    Prefer manifest-provided paths when available; this is only used when
    the export hasn't yet recorded rendered endpoint variants.
    """

    cache_key = hashlib.sha256(endpoint_url.encode("utf-8")).hexdigest()[:12]
    safe_title = _safe_filename_component(_guess_title_from_url(endpoint_url))
    stem = f"{safe_title}--{cache_key}".replace(" ", "-")
    return f"pages/{stem}.resp.md"


def _normalize_endpoint(url: str) -> str | None:
    url = url.strip().strip("\"'<>[](){}.,;:")

    # Heuristic cleanup: when scanning Markdown/HTML, we can end up matching
    # a URL immediately followed by ")[text](...)" with no whitespace.
    # Prefer trimming at the first ")" if it looks like a Markdown boundary.
    if url.lower().startswith(("http://", "https://")):
        close_paren = url.find(")")
        if close_paren != -1 and close_paren + 1 < len(url):
            nxt = url[close_paren + 1]
            if nxt in {"[", "("}:
                url = url[:close_paren]

        close_bracket = url.find("]")
        if close_bracket != -1 and close_bracket + 1 < len(url):
            nxt = url[close_bracket + 1]
            if nxt in {"(", "["}:
                url = url[:close_bracket]
    try:
        parsed = urlparse(url)
    except ValueError:
        return None

    scheme = (parsed.scheme or "https").lower()
    netloc = (parsed.netloc or "data.uspto.gov").lower()
    if netloc != "data.uspto.gov":
        return None

    path = parsed.path or ""
    if not path.startswith("/apis/"):
        return None

    parsed = parsed._replace(
        scheme="https" if scheme in {"http", "https"} else "https",
        netloc="data.uspto.gov",
        fragment="",
    )
    return urlunparse(parsed)


def extract_api_endpoints(
    text: str,
    *,
    source_url: str,
) -> list[ApiEndpointFinding]:
    found: list[ApiEndpointFinding] = []

    for m in _ABS_APIS_RE.finditer(text):
        normalized = _normalize_endpoint(m.group(0))
        if normalized:
            found.append(ApiEndpointFinding(endpoint=normalized, source_url=source_url))

    for m in _REL_APIS_RE.finditer(text):
        normalized = _normalize_endpoint(_USPTO_APIS_BASE + m.group(1))
        if normalized:
            found.append(ApiEndpointFinding(endpoint=normalized, source_url=source_url))

    return found


def _iter_ingested_pages(manifest_jsonl: Path) -> Iterable[dict]:
    with manifest_jsonl.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            yield evt


def _wrap_source_bullets(
    *,
    source_url: str,
    max_width: int = 100,
) -> list[str]:
    prefix = "  - source: "
    if len(prefix) + len(source_url) <= max_width:
        return [f"{prefix}{source_url}"]

    wrapped = textwrap.wrap(
        source_url,
        width=max_width - len(prefix),
        break_long_words=True,
        break_on_hyphens=False,
    )
    if not wrapped:
        return [f"{prefix}{source_url}"]

    lines = [f"{prefix}{wrapped[0]}"]
    continuation = " " * len(prefix)
    lines.extend([f"{continuation}{part}" for part in wrapped[1:]])
    return lines


@dataclass(frozen=True)
class ApiReportData:
    export_dir: Path
    scanned_pages: int
    endpoints_to_sources: dict[str, list[str]]
    endpoints_to_resp_md: dict[str, str]

    @property
    def endpoints(self) -> list[str]:
        return sorted(self.endpoints_to_sources.keys())


def collect_apis_report_data(*, export_dir: Path) -> ApiReportData:
    export_dir = export_dir.resolve()
    manifest_jsonl = export_dir / "manifest.jsonl"
    if not manifest_jsonl.exists():
        raise FileNotFoundError(f"Missing manifest.jsonl in: {export_dir}")

    endpoints_to_sources: dict[str, set[str]] = {}
    endpoints_to_resp_md: dict[str, str] = {}
    scanned_pages = 0

    for evt in _iter_ingested_pages(manifest_jsonl):
        if evt.get("kind") == "rendered_endpoint_variants":
            url = str(evt.get("url") or "")
            normalized = _normalize_endpoint(url)
            if not normalized:
                continue
            paths = evt.get("paths") or {}
            resp_md = paths.get("resp_md")
            if isinstance(resp_md, str) and resp_md:
                candidate = resp_md
                candidate_path = export_dir / candidate
                existing = endpoints_to_resp_md.get(normalized)

                if candidate_path.exists():
                    endpoints_to_resp_md[normalized] = candidate
                elif not existing:
                    endpoints_to_resp_md[normalized] = candidate

        if evt.get("kind") not in {"ingested_local", "fetched"}:
            continue

        source_url = str(evt.get("url") or "")
        if not source_url:
            continue

        paths = evt.get("paths") or {}
        candidates: list[Path] = []
        for key in ("page_md", "raw"):
            rel = paths.get(key)
            if isinstance(rel, str) and rel:
                candidates.append(export_dir / rel)

        if not candidates:
            continue

        scanned_pages += 1
        for p in candidates:
            if not p.exists() or not p.is_file():
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            for finding in extract_api_endpoints(text, source_url=source_url):
                endpoints_to_sources.setdefault(finding.endpoint, set()).add(
                    finding.source_url
                )

    materialized: dict[str, list[str]] = {
        endpoint: sorted(sources) for endpoint, sources in endpoints_to_sources.items()
    }
    return ApiReportData(
        export_dir=export_dir,
        scanned_pages=scanned_pages,
        endpoints_to_sources=materialized,
        endpoints_to_resp_md=endpoints_to_resp_md,
    )


def write_apis_report(
    *,
    export_dir: Path,
    report_path: Path | None = None,
) -> Path:
    """Scan an existing export directory and write a deduped /apis/ report.

    Expected input layout (as produced by extract_ocr crawl/seed ingestion):
      - manifest.jsonl
      - raw/
      - pages/

    Output: a Markdown report listing unique endpoints and source page URLs.
    """

    report = collect_apis_report_data(export_dir=export_dir)
    if report_path is None:
        report_path = report.export_dir / "apis_endpoints_report.md"

    endpoints = report.endpoints
    generated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    lines: list[str] = []
    lines.append("# data.uspto.gov /apis/ endpoint inventory")
    lines.append("")
    lines.append(f"Generated: {generated_at}")
    lines.append(f"Export dir: {report.export_dir}")
    lines.append(f"Pages scanned (manifest events): {report.scanned_pages}")
    lines.append(f"Unique endpoints: {len(endpoints)}")
    lines.append("")

    for endpoint in endpoints:
        sources = report.endpoints_to_sources.get(endpoint) or []
        resp_md = report.endpoints_to_resp_md.get(endpoint)
        if not resp_md:
            resp_md = _fallback_resp_md_relpath(endpoint)

        resp_md_path = report.export_dir / resp_md
        missing_marker = ""
        if not resp_md_path.exists():
            missing_marker = " (MISSING resp.md)"

        lines.append(f"- [{endpoint}]({resp_md}){missing_marker}")
        for src in sources:
            lines.extend(_wrap_source_bullets(source_url=src))

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report_path
