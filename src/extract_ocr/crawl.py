from __future__ import annotations

import hashlib
import html as html_lib
import io
import json
import re
import time
from collections import Counter, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse
from xml.dom import minidom
from xml.parsers.expat import ExpatError

import requests
from bs4 import BeautifulSoup

from .cache import cache_paths, read_cached, write_cached
from .citations import CitationItem
from .content import ContentKind, is_waf_challenge, sniff_kind
from .convert.html_to_md import extract_title, html_to_markdown
from .http_client import HttpClient
from .manifest import ManifestWriter, relpath_posix, utc_iso
from .robots import RobotsCache, RobotsRules
from .state import CrawlState
from .urls import UrlScope, normalize_url

_INVALID_FILENAME_CHARS = re.compile(r"[<>:\"/\\|?*\x00-\x1F]")


def _safe_filename_component(text: str) -> str:
    cleaned = _INVALID_FILENAME_CHARS.sub("-", (text or "").strip())
    cleaned = cleaned.strip(". ")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        cleaned = "page"
    return cleaned[:150]


def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text("\n")
    lines = [ln.strip() for ln in text.splitlines()]
    out: list[str] = []
    blank_run = 0
    for ln in lines:
        if not ln:
            blank_run += 1
            if blank_run <= 1:
                out.append("")
            continue
        blank_run = 0
        out.append(ln)
    return "\n".join(out).strip() + "\n"


def _truncate_text(text: str, *, max_chars: int = 400_000) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars].rstrip("\n") + "\n\n[TRUNCATED]\n", True


def _format_non_html_for_markdown(
    *,
    kind: ContentKind,
    body: bytes,
    content_type: str | None,
) -> tuple[str, str]:
    """Return (rendered_text, fence_language)."""

    _ = content_type

    if kind == ContentKind.JSON:
        try:
            decoded = body.decode("utf-8", errors="strict")
            obj = json.loads(decoded)
            pretty = json.dumps(obj, indent=2, ensure_ascii=False) + "\n"
            return pretty, "json"
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            text = body.decode("utf-8", errors="replace")
            return text, "text"

    if kind == ContentKind.XML:
        text = body.decode("utf-8", errors="replace")
        try:
            doc = minidom.parseString(text.encode("utf-8"))
            pretty = doc.toprettyxml(indent="  ")
            # minidom can be noisy with blank lines; normalize lightly.
            lines = [ln.rstrip() for ln in pretty.splitlines() if ln.strip()]
            return "\n".join(lines).strip() + "\n", "xml"
        except (ExpatError, UnicodeEncodeError, ValueError):
            return text.strip() + "\n", "xml"

    if kind == ContentKind.PDF:
        try:
            from pypdf import PdfReader  # type: ignore[import-not-found]
            from pypdf.errors import (  # type: ignore[import-not-found]
                PdfReadError,
            )
        except ImportError:
            return "(PDF captured; install pypdf to extract text.)\n", "text"

        try:
            reader = PdfReader(io.BytesIO(body))
        except (PdfReadError, ValueError, OSError):
            return "(PDF captured, but failed to parse it.)\n", "text"

        parts: list[str] = []
        for page in reader.pages:
            try:
                page_text = page.extract_text() or ""
            except (ValueError, RuntimeError, AttributeError):
                page_text = ""
            if page_text:
                parts.append(page_text)

        text = "\n\n".join(parts).strip() + "\n"
        if text.strip():
            return text, "text"
        return "(No extractable text found in PDF.)\n", "text"

    text = body.decode("utf-8", errors="replace")
    return text, "text"


def ensure_export_html_variants(*, export_dir: Path) -> int:
    """(Re)generate per-page MD/HTML/TXT/JSON variants for HTML documents.

    This is a filesystem-level normalization step used when an export already
    contains raw HTML (and possibly markdown) but needs consistent sidecar
    formats for each document.
    """

    export_dir = export_dir.resolve()
    manifest_jsonl = export_dir / "manifest.jsonl"
    if not manifest_jsonl.exists():
        return 0

    pages_dir = export_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    manifest = ManifestWriter(export_dir)

    rendered = 0
    with manifest_jsonl.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue

            if evt.get("kind") not in {"ingested_local", "fetched", "blocked"}:
                continue

            url = str(evt.get("url") or "")
            if not url:
                continue

            paths = evt.get("paths") or {}
            raw_rel = paths.get("raw")
            if not isinstance(raw_rel, str) or not raw_rel:
                continue

            content_type = evt.get("content_type")
            if not (
                str(content_type or "").lower().startswith("text/html")
                or raw_rel.lower().endswith(".html")
            ):
                continue

            raw_path = export_dir / raw_rel
            if not raw_path.exists() or not raw_path.is_file():
                continue

            try:
                raw = raw_path.read_bytes()
            except OSError:
                continue

            html_text = raw.decode("utf-8", errors="replace")
            title = str(evt.get("title") or "")
            if not title:
                title = extract_title(html_text)

            md_rel = paths.get("page_md")
            stem = None
            if isinstance(md_rel, str) and md_rel:
                stem = Path(md_rel).stem

            cache_key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
            safe_title = _safe_filename_component(title)
            stem = stem or f"{safe_title}--{cache_key}".replace(" ", "-")

            md_path = pages_dir / f"{stem}.md"
            html_path = pages_dir / f"{stem}.html"
            txt_path = pages_dir / f"{stem}.txt"
            meta_path = pages_dir / f"{stem}.json"

            md_text = html_to_markdown(html_text, source_url=url)
            md_path.write_text(md_text, encoding="utf-8", newline="\n")
            html_path.write_text(html_text, encoding="utf-8", newline="\n")
            txt_path.write_text(
                _html_to_text(html_text),
                encoding="utf-8",
                newline="\n",
            )

            started_at = str(evt.get("at") or "")
            status_code = evt.get("status_code")
            meta = {
                "url": url,
                "title": title,
                "generated_at": utc_iso(),
                "started_at": started_at,
                "status_code": status_code,
                "content_type": content_type,
                "paths": {
                    "raw": raw_rel,
                    "page_md": relpath_posix(md_path, export_dir),
                    "page_html": relpath_posix(html_path, export_dir),
                    "page_txt": relpath_posix(txt_path, export_dir),
                    "page_json": relpath_posix(meta_path, export_dir),
                },
            }
            meta_path.write_text(
                json.dumps(meta, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
                newline="\n",
            )

            manifest.append(
                {
                    "kind": "rendered_variants",
                    "url": url,
                    "title": title,
                    "paths": meta["paths"],
                }
            )
            rendered += 1

    return rendered


def ensure_export_non_html_variants(*, export_dir: Path) -> int:
    """(Re)generate per-page MD/TXT/JSON variants.

    Targets JSON/XML/PDF/TEXT documents.
    """

    export_dir = export_dir.resolve()
    manifest_jsonl = export_dir / "manifest.jsonl"
    if not manifest_jsonl.exists():
        return 0

    pages_dir = export_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    manifest = ManifestWriter(export_dir)

    rendered = 0
    with manifest_jsonl.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue

            if evt.get("kind") not in {"ingested_local", "fetched"}:
                continue

            url = str(evt.get("url") or "")
            if not url:
                continue

            paths = evt.get("paths") or {}
            raw_rel = paths.get("raw")
            if not isinstance(raw_rel, str) or not raw_rel:
                continue

            raw_path = export_dir / raw_rel
            if not raw_path.exists() or not raw_path.is_file():
                continue

            try:
                body = raw_path.read_bytes()
            except OSError:
                continue

            content_type = evt.get("content_type")
            kind = sniff_kind(
                url,
                content_type=str(content_type or ""),
                body=body,
            )
            if kind not in {
                ContentKind.JSON,
                ContentKind.XML,
                ContentKind.PDF,
                ContentKind.TEXT,
            }:
                continue

            status_code = evt.get("status_code")
            if status_code is not None:
                try:
                    sc = int(status_code)
                except (TypeError, ValueError):
                    sc = 0
                if sc and not (200 <= sc < 400):
                    continue

            title = str(evt.get("title") or "")
            if not title:
                title = _guess_title_from_url(url)

            md_rel = paths.get("page_md")
            stem = None
            if isinstance(md_rel, str) and md_rel:
                stem = Path(md_rel).stem

            cache_key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
            safe_title = _safe_filename_component(title)
            stem = stem or f"{safe_title}--{cache_key}".replace(" ", "-")

            md_path = pages_dir / f"{stem}.md"
            txt_path = pages_dir / f"{stem}.txt"
            meta_path = pages_dir / f"{stem}.json"

            rendered_text, fence = _format_non_html_for_markdown(
                kind=kind,
                body=body,
                content_type=str(content_type or ""),
            )
            rendered_text, truncated = _truncate_text(rendered_text)

            txt_path.write_text(rendered_text, encoding="utf-8", newline="\n")
            md_path.write_text(
                "\n".join(
                    [
                        f"# {title}",
                        "",
                        f"URL: {url}",
                        f"Content-Type: {content_type}",
                        f"Kind: {kind.value}",
                        "",
                        "```" + fence,
                        rendered_text.rstrip("\n"),
                        "```",
                        "" if not truncated else "(Output truncated.)",
                        "",
                    ]
                ),
                encoding="utf-8",
                newline="\n",
            )

            started_at = str(evt.get("at") or "")
            meta = {
                "url": url,
                "title": title,
                "generated_at": utc_iso(),
                "started_at": started_at,
                "status_code": status_code,
                "content_type": content_type,
                "kind": kind.value,
                "paths": {
                    "raw": raw_rel,
                    "page_md": relpath_posix(md_path, export_dir),
                    "page_txt": relpath_posix(txt_path, export_dir),
                    "page_json": relpath_posix(meta_path, export_dir),
                },
            }
            meta_path.write_text(
                json.dumps(meta, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
                newline="\n",
            )

            manifest.append(
                {
                    "kind": "rendered_non_html_variants",
                    "url": url,
                    "title": title,
                    "paths": meta["paths"],
                }
            )
            rendered += 1

    return rendered


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


def _format_response_as_text(
    *,
    body: bytes,
    content_type: str | None,
) -> tuple[str, dict]:
    """Return (text_for_display, json_payload_for_resp_json)."""

    ct = str(content_type or "")
    kind = ct.split(";")[0].strip().lower()

    if kind == "application/json" or kind.endswith("+json"):
        try:
            decoded = body.decode("utf-8", errors="strict")
            obj = json.loads(decoded)
            pretty = json.dumps(obj, indent=2, ensure_ascii=False) + "\n"
            return pretty, {"type": "json", "value": obj}
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            text = body.decode("utf-8", errors="replace")
            return text, {"type": "text", "value": text}

    text = body.decode("utf-8", errors="replace")
    return text, {"type": "text", "value": text}


def ensure_export_api_endpoint_variants(*, export_dir: Path) -> int:
    """Generate MD/HTML/TXT/JSON *response* variants for fetched endpoints.

    This is intentionally additive: it does not overwrite the existing
    pages/<stem>.json metadata files. Instead it writes:
      - pages/<stem>.resp.md
      - pages/<stem>.resp.html
      - pages/<stem>.resp.txt
      - pages/<stem>.resp.json
    """

    export_dir = export_dir.resolve()
    manifest_jsonl = export_dir / "manifest.jsonl"
    if not manifest_jsonl.exists():
        return 0

    pages_dir = export_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    manifest = ManifestWriter(export_dir)

    rendered = 0
    with manifest_jsonl.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue

            if evt.get("kind") not in {"ingested_local", "fetched", "blocked"}:
                continue

            url = str(evt.get("url") or "")
            if not url:
                continue

            # Only generate response variants for API endpoints.
            try:
                parsed = urlparse(url)
            except ValueError:
                continue
            if (parsed.netloc or "").lower() != "data.uspto.gov":
                continue
            if not (parsed.path or "").startswith("/apis/"):
                continue

            paths = evt.get("paths") or {}
            raw_rel = paths.get("raw")
            if not isinstance(raw_rel, str) or not raw_rel:
                continue

            raw_path = export_dir / raw_rel
            if not raw_path.exists() or not raw_path.is_file():
                continue

            try:
                body = raw_path.read_bytes()
            except OSError:
                continue

            content_type = evt.get("content_type")
            status_code = evt.get("status_code")
            title = str(evt.get("title") or "")
            if not title:
                title = _guess_title_from_url(url)

            # Match the existing filename style for stability.
            cache_key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
            safe_title = _safe_filename_component(title)
            stem = f"{safe_title}--{cache_key}".replace(" ", "-")

            resp_md = pages_dir / f"{stem}.resp.md"
            resp_html = pages_dir / f"{stem}.resp.html"
            resp_txt = pages_dir / f"{stem}.resp.txt"
            resp_json = pages_dir / f"{stem}.resp.json"

            text, payload = _format_response_as_text(
                body=body,
                content_type=str(content_type or ""),
            )

            resp_txt.write_text(text, encoding="utf-8", newline="\n")
            resp_md.write_text(
                "\n".join(
                    [
                        f"# {title}",
                        "",
                        f"URL: {url}",
                        f"Content-Type: {content_type}",
                        f"Status: {status_code}",
                        "",
                        "```",
                        text.rstrip("\n"),
                        "```",
                        "",
                    ]
                ),
                encoding="utf-8",
                newline="\n",
            )
            resp_html.write_text(
                "\n".join(
                    [
                        "<!doctype html>",
                        '<meta charset="utf-8">',
                        f"<title>{html_lib.escape(title)}</title>",
                        "<pre>",
                        html_lib.escape(text),
                        "</pre>",
                        "",
                    ]
                ),
                encoding="utf-8",
                newline="\n",
            )

            resp_obj = {
                "url": url,
                "title": title,
                "generated_at": utc_iso(),
                "status_code": status_code,
                "content_type": content_type,
                "raw_path": raw_rel,
                "payload": payload,
            }
            resp_json.write_text(
                json.dumps(resp_obj, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
                newline="\n",
            )

            manifest.append(
                {
                    "kind": "rendered_endpoint_variants",
                    "url": url,
                    "title": title,
                    "paths": {
                        "raw": raw_rel,
                        "resp_md": relpath_posix(resp_md, export_dir),
                        "resp_html": relpath_posix(resp_html, export_dir),
                        "resp_txt": relpath_posix(resp_txt, export_dir),
                        "resp_json": relpath_posix(resp_json, export_dir),
                    },
                }
            )
            rendered += 1

    return rendered


def extract_links_from_html(html: str, *, page_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")

    def _attr_text(val: object) -> str:
        if isinstance(val, list):
            if not val:
                return ""
            return str(val[0])
        return str(val or "")

    base_href = None
    base = soup.find("base")
    if base is not None:
        base_href = _attr_text(base.get("href")).strip() or None

    effective_base = page_url
    if base_href is not None:
        effective_base = urljoin(page_url, base_href)

    out: list[str] = []
    for a in soup.select("a[href]"):
        href = _attr_text(a.get("href")).strip()
        if not href:
            continue
        if href.startswith("#"):
            continue
        if href.lower().startswith("mailto:"):
            continue
        abs_url = urljoin(effective_base, href)
        abs_url = normalize_url(abs_url)
        out.append(abs_url)

    return out


@dataclass
class CrawlConfig:
    out_dir: Path
    scope: UrlScope
    max_pages: int = 200
    max_depth: int = 3
    per_host_delay_s: float = 0.5
    respect_robots: bool = True
    refresh_cache: bool = False


class Crawler:
    def __init__(
        self,
        *,
        http: HttpClient,
        config: CrawlConfig,
    ) -> None:
        self.http = http
        self.cfg = config

        self.out_dir = self.cfg.out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)

        self.cache_dir = self.out_dir / ".cache"
        self.raw_dir = self.out_dir / "raw"
        self.pages_dir = self.out_dir / "pages"
        self.state = CrawlState(self.out_dir / ".state")
        self.robots_cache = RobotsCache(self.state.state_dir / "robots")
        self.manifest = ManifestWriter(self.out_dir)

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.pages_dir.mkdir(parents=True, exist_ok=True)

        self._last_fetch_at_by_host: dict[str, float] = {}
        self._stats: Counter[str] = Counter()
        self._citations: list[CitationItem] = []

    def _pacing_sleep(self, host: str) -> None:
        last = self._last_fetch_at_by_host.get(host)
        if last is None:
            return
        elapsed = time.time() - last
        if elapsed < self.cfg.per_host_delay_s:
            time.sleep(self.cfg.per_host_delay_s - elapsed)

    def _fetch_robots(self, host: str) -> RobotsRules | None:
        cached = self.robots_cache.load(host)
        if cached is not None:
            return cached

        robots_url = f"https://{host}/robots.txt"
        try:
            res = self.http.get(robots_url)
            if res.status_code >= 400:
                return None
            text = res.body.decode("utf-8", errors="replace")
            self.robots_cache.store(host, text)
            return RobotsRules(text)
        except (
            OSError,
            UnicodeDecodeError,
            requests.RequestException,
            RuntimeError,
        ):
            return None

    def _should_fetch(self, url: str) -> tuple[bool, str | None]:
        if not self.cfg.scope.is_allowed(url):
            return False, "offsite"

        if not self.cfg.respect_robots:
            return True, None

        host = (urlparse(url).hostname or "").lower()
        if not host:
            return False, "no_host"

        rules = self._fetch_robots(host)
        if rules is None:
            return True, None

        if not rules.can_fetch(url):
            return False, "robots"

        return True, None

    def _cache_key(self, url: str) -> str:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]

    def _write_page_variants(
        self,
        *,
        url: str,
        title: str,
        html_text: str,
        raw_path: Path,
        started_at: str,
        status_code: int | None = None,
        content_type: str | None = None,
        md_stem: str | None = None,
    ) -> dict[str, str]:
        cache_key = self._cache_key(url)
        safe_title = _safe_filename_component(title)
        stem = md_stem or f"{safe_title}--{cache_key}".replace(" ", "-")

        md_path = self.pages_dir / f"{stem}.md"
        html_path = self.pages_dir / f"{stem}.html"
        txt_path = self.pages_dir / f"{stem}.txt"
        meta_path = self.pages_dir / f"{stem}.json"

        md_text = html_to_markdown(html_text, source_url=url)
        md_path.write_text(md_text, encoding="utf-8", newline="\n")
        html_path.write_text(html_text, encoding="utf-8", newline="\n")
        txt_path.write_text(
            _html_to_text(html_text),
            encoding="utf-8",
            newline="\n",
        )

        meta = {
            "url": url,
            "title": title,
            "generated_at": utc_iso(),
            "started_at": started_at,
            "status_code": status_code,
            "content_type": content_type,
            "paths": {
                "raw": relpath_posix(raw_path, self.out_dir),
                "page_md": relpath_posix(md_path, self.out_dir),
                "page_html": relpath_posix(html_path, self.out_dir),
                "page_txt": relpath_posix(txt_path, self.out_dir),
                "page_json": relpath_posix(meta_path, self.out_dir),
            },
        }
        meta_path.write_text(
            json.dumps(meta, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )

        return {
            "raw": relpath_posix(raw_path, self.out_dir),
            "page_md": relpath_posix(md_path, self.out_dir),
            "page_html": relpath_posix(html_path, self.out_dir),
            "page_txt": relpath_posix(txt_path, self.out_dir),
            "page_json": relpath_posix(meta_path, self.out_dir),
        }

    def _write_non_html_variants(
        self,
        *,
        url: str,
        title: str,
        kind: ContentKind,
        body: bytes,
        raw_path: Path,
        started_at: str,
        status_code: int | None = None,
        content_type: str | None = None,
    ) -> dict[str, str]:
        cache_key = self._cache_key(url)
        safe_title = _safe_filename_component(title)
        stem = f"{safe_title}--{cache_key}".replace(" ", "-")

        md_path = self.pages_dir / f"{stem}.md"
        txt_path = self.pages_dir / f"{stem}.txt"
        meta_path = self.pages_dir / f"{stem}.json"

        rendered_text, fence = _format_non_html_for_markdown(
            kind=kind,
            body=body,
            content_type=content_type,
        )
        rendered_text, truncated = _truncate_text(rendered_text)

        txt_path.write_text(rendered_text, encoding="utf-8", newline="\n")
        md_path.write_text(
            "\n".join(
                [
                    f"# {title}",
                    "",
                    f"URL: {url}",
                    f"Content-Type: {content_type}",
                    f"Status: {status_code}",
                    f"Kind: {kind.value}",
                    "",
                    "```" + fence,
                    rendered_text.rstrip("\n"),
                    "```",
                    "" if not truncated else "(Output truncated.)",
                    "",
                ]
            ),
            encoding="utf-8",
            newline="\n",
        )

        meta = {
            "url": url,
            "title": title,
            "generated_at": utc_iso(),
            "started_at": started_at,
            "status_code": status_code,
            "content_type": content_type,
            "kind": kind.value,
            "paths": {
                "raw": relpath_posix(raw_path, self.out_dir),
                "page_md": relpath_posix(md_path, self.out_dir),
                "page_txt": relpath_posix(txt_path, self.out_dir),
                "page_json": relpath_posix(meta_path, self.out_dir),
            },
        }
        meta_path.write_text(
            json.dumps(meta, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
            newline="\n",
        )

        return {
            "raw": relpath_posix(raw_path, self.out_dir),
            "page_md": relpath_posix(md_path, self.out_dir),
            "page_txt": relpath_posix(txt_path, self.out_dir),
            "page_json": relpath_posix(meta_path, self.out_dir),
        }

    def _store_raw(self, _url: str, *, kind: ContentKind, body: bytes) -> Path:
        # Keep a stable, content-addressed filename to enable dedupe.
        sha = hashlib.sha256(body).hexdigest()[:16]
        ext = {
            ContentKind.HTML: ".html",
            ContentKind.JSON: ".json",
            ContentKind.XML: ".xml",
            ContentKind.PDF: ".pdf",
            ContentKind.TEXT: ".txt",
            ContentKind.ZIP: ".zip",
        }.get(kind, ".bin")

        kind_dir = self.raw_dir / kind.value
        kind_dir.mkdir(parents=True, exist_ok=True)
        path = kind_dir / f"{sha}{ext}"
        if not path.exists():
            path.write_bytes(body)
        return path

    def _extract_links(self, html: str, *, page_url: str) -> list[str]:
        return extract_links_from_html(html, page_url=page_url)

    def ingest_local_html(self, *, url: str, body: bytes) -> None:
        """Ingest a browser-saved HTML page as a local artifact.

        This is used for sites protected by JS challenges where direct HTTP
        fetching is blocked, but the user can provide HTML snapshots.
        """

        # Store raw HTML and render variants.
        raw_path = self._store_raw(url, kind=ContentKind.HTML, body=body)
        html_text = body.decode("utf-8", errors="replace")
        title = extract_title(html_text)
        started_at = utc_iso()
        paths = self._write_page_variants(
            url=url,
            title=title,
            html_text=html_text,
            raw_path=raw_path,
            started_at=started_at,
            status_code=None,
            content_type="text/html",
        )

        md_path = self.out_dir / paths["page_md"]

        accessed = utc_iso()[:10]
        self._citations.append(
            CitationItem(
                title=title,
                url=url,
                accessed=accessed,
                local_path=relpath_posix(md_path, self.out_dir),
            )
        )

        # Mark as done so a subsequent crawl() won't try to refetch it.
        self.state.append_line(self.state.done_path, url)

        self._stats["ingested_local"] += 1
        self.manifest.append(
            {
                "kind": "ingested_local",
                "url": url,
                "content_type": "text/html",
                "title": title,
                "paths": paths,
            }
        )

    def crawl(self, seeds: Iterable[str], *, resume: bool = True) -> dict:
        queue = deque((normalize_url(s), 0) for s in seeds)

        done = self.state.load_set(self.state.done_path)
        failed = self.state.load_set(self.state.failed_path)

        # Restore queue if present.
        if resume:
            restored = self.state.load_queue()
            if restored:
                queue = deque((normalize_url(u), 0) for u in restored)

        enqueued: set[str] = set(u for u, _ in queue)

        pages_fetched = 0
        started_at = utc_iso()

        while queue and pages_fetched < self.cfg.max_pages:
            url, depth = queue.popleft()
            if url in done or url in failed:
                continue

            ok, blocked_reason = self._should_fetch(url)
            if not ok:
                self._stats["blocked"] += 1
                self.state.append_line(self.state.done_path, url)
                done.add(url)
                self.manifest.append(
                    {"kind": "blocked", "url": url, "reason": blocked_reason}
                )
                continue

            host = (urlparse(url).hostname or "").lower()
            self._pacing_sleep(host)

            # Cache behavior.
            cache_entry = cache_paths(self.cache_dir, key=self._cache_key(url))
            body, meta = (None, None)
            if (
                cache_entry.body_path.exists()
                and cache_entry.meta_path.exists()
                and not self.cfg.refresh_cache
            ):
                body, meta = read_cached(cache_entry)

            if body is None:
                try:
                    res = self.http.get(url)
                except (requests.RequestException, RuntimeError) as e:
                    self._stats["error"] += 1
                    failed.add(url)
                    self.state.append_line(self.state.failed_path, url)
                    self.manifest.append(
                        {
                            "kind": "error",
                            "url": url,
                            "error": str(e),
                        }
                    )
                    continue
                body = res.body
                meta = {
                    "status_code": res.status_code,
                    "headers": res.headers,
                    "final_url": res.final_url,
                }

                # Write cache even for non-200; it's useful evidence.
                write_cached(cache_entry, res)

            self._last_fetch_at_by_host[host] = time.time()

            status = int((meta or {}).get("status_code") or 0)
            headers = (meta or {}).get("headers") or {}
            content_type = headers.get("Content-Type")

            kind = sniff_kind(url, content_type=content_type, body=body)

            # Detect WAF challenge pages and mark them as blocked
            # (don’t parse links).
            if is_waf_challenge(body, content_type=content_type):
                self._stats["blocked_waf"] += 1
                raw_path = self._store_raw(
                    url,
                    kind=ContentKind.HTML,
                    body=body,
                )
                self.manifest.append(
                    {
                        "kind": "blocked",
                        "blocked_by": "aws_waf",
                        "url": url,
                        "status_code": status,
                        "content_type": content_type,
                        "paths": {
                            "raw": relpath_posix(raw_path, self.out_dir),
                        },
                    }
                )
                done.add(url)
                self.state.append_line(self.state.done_path, url)
                continue

            raw_path = self._store_raw(url, kind=kind, body=body)

            event: dict = {
                "kind": "fetched",
                "url": url,
                "status_code": status,
                "content_type": content_type,
                "paths": {"raw": relpath_posix(raw_path, self.out_dir)},
            }

            if kind == ContentKind.HTML and status and 200 <= status < 400:
                html_text = body.decode("utf-8", errors="replace")
                title = extract_title(html_text)
                paths = self._write_page_variants(
                    url=url,
                    title=title,
                    html_text=html_text,
                    raw_path=raw_path,
                    started_at=started_at,
                    status_code=status,
                    content_type=content_type,
                )

                event["paths"].update(paths)
                event["title"] = title

                md_path = self.out_dir / paths["page_md"]

                self._citations.append(
                    CitationItem(
                        title=title,
                        url=url,
                        accessed=started_at[:10],
                        local_path=relpath_posix(md_path, self.out_dir),
                    )
                )

                if depth < self.cfg.max_depth:
                    for link in self._extract_links(html_text, page_url=url):
                        if link in enqueued or link in done or link in failed:
                            continue
                        if not self.cfg.scope.is_allowed(link):
                            continue
                        # Guard against URL explosion; skip long paths.
                        if len(urlparse(link).path) > 500:
                            continue
                        enqueued.add(link)
                        queue.append((link, depth + 1))

            if (
                kind
                in {
                    ContentKind.JSON,
                    ContentKind.XML,
                    ContentKind.PDF,
                    ContentKind.TEXT,
                }
                and status
                and 200 <= status < 400
            ):
                title = _guess_title_from_url(url)
                paths = self._write_non_html_variants(
                    url=url,
                    title=title,
                    kind=kind,
                    body=body,
                    raw_path=raw_path,
                    started_at=started_at,
                    status_code=status,
                    content_type=content_type,
                )
                event["paths"].update(paths)
                event["title"] = title

                md_path = self.out_dir / paths["page_md"]
                self._citations.append(
                    CitationItem(
                        title=title,
                        url=url,
                        accessed=started_at[:10],
                        local_path=relpath_posix(md_path, self.out_dir),
                    )
                )

            self._stats["fetched"] += 1
            pages_fetched += 1

            done.add(url)
            self.state.append_line(self.state.done_path, url)
            self.manifest.append(event)

            # Persist queue periodically for resumability.
            if pages_fetched % 25 == 0:
                self.state.save_queue([u for u, _ in queue])

        # Final queue save.
        self.state.save_queue([u for u, _ in queue])

        summary = {
            "started_at": started_at,
            "finished_at": utc_iso(),
            "config": {
                "max_pages": self.cfg.max_pages,
                "max_depth": self.cfg.max_depth,
                "per_host_delay_s": self.cfg.per_host_delay_s,
                "respect_robots": self.cfg.respect_robots,
                "refresh_cache": self.cfg.refresh_cache,
                "allow_host_suffixes": list(self.cfg.scope.allow_host_suffixes),
                "follow_offsite": self.cfg.scope.follow_offsite,
            },
            "stats": dict(self._stats),
            "remaining_queue": len(queue),
        }
        self.manifest.write_summary(summary)
        return summary

    @property
    def citations(self) -> list[CitationItem]:
        return list(self._citations)
