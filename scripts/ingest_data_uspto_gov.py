"""Ingest https://data.uspto.gov into offline artifacts.

Also crawls linked USPTO hosts.

Design goals (aligned with this repo):
- ToS-friendly: respects robots.txt by default.
- Resumable/idempotent: stable filenames, on-disk cache, append-only manifest.
- Rate-limited: per-host pacing + Retry-After handling.
- Output-first: saves artifacts to disk; does not print page bodies.

This script intentionally focuses on *ingestion* (mirroring + indexing). It
not attempt to exhaustively transform content beyond optional HTML→Markdown
conversion.

Typical usage:
  .venv312\\Scripts\\python.exe scripts\\ingest_data_uspto_gov.py \
        --out "docs/USPTO ODP/export/data-uspto-gov"

If you have an ODP API key and want to crawl api.uspto.gov JSON endpoints when
they are discovered, export it first:
  $env:USPTO_ODP_API_KEY = "..."

Notes:
- This can follow linked URLs, but by default it stays within the uspto.gov
    suffix.
- Large crawls can take a long time; start with --max-pages 200 for a smoke
    run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from collections import Counter, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import ParseResult, urljoin, urlparse, urlunparse

import requests  # type: ignore[import-untyped]
from bs4 import BeautifulSoup
from markdownify import markdownify as md

TRANSIENT_HTTP_STATUSES = {429, 500, 502, 503, 504}


def _utc_iso() -> str:
    # No dependency; good enough for manifests.
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _normalize_url(raw_url: str) -> str:
    """Normalize URLs for de-duplication.

    - Lowercases scheme + hostname.
    - Strips fragments.
    - Drops trivial tracking query params we know about.
    """

    parsed: ParseResult = urlparse(raw_url)
    scheme = (parsed.scheme or "").lower()
    netloc = (parsed.netloc or "").lower()

    # Drop trivial tracking flags that cause duplicates.
    query = parsed.query
    if query.strip().lower() in {"agt=index"}:
        query = ""

    parsed = parsed._replace(
        scheme=scheme,
        netloc=netloc,
        fragment="",
        query=query,
    )
    return urlunparse(parsed)


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]


def _retry_after_seconds(headers: dict[str, str]) -> float | None:
    retry_after = headers.get("Retry-After")
    if not retry_after:
        return None
    try:
        return float(retry_after)
    except ValueError:
        return None


def _safe_filename_piece(text: str, *, max_len: int = 80) -> str:
    text = text.strip()
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    if not text:
        return "untitled"
    return text[:max_len]


def _guess_extension(url: str, content_type: str | None) -> str:
    # Prefer content-type.
    if content_type:
        ct = content_type.split(";")[0].strip().lower()
        if ct in {"text/html", "application/xhtml+xml"}:
            return ".html"
        if ct in {"application/json", "text/json"}:
            return ".json"
        if ct in {"application/pdf"}:
            return ".pdf"
        if ct in {"text/plain"}:
            return ".txt"
        if ct in {"application/xml", "text/xml"}:
            return ".xml"
        if ct in {"application/yaml", "text/yaml", "application/x-yaml"}:
            return ".yaml"

    # Fall back to URL path.
    path = urlparse(url).path.lower()
    for ext in [
        ".html",
        ".htm",
        ".json",
        ".pdf",
        ".xml",
        ".yaml",
        ".yml",
        ".txt",
    ]:
        if path.endswith(ext):
            return ext
    return ".bin"


@dataclass(frozen=True)
class CachedResponse:
    url: str
    final_url: str
    status_code: int
    headers: dict[str, str]
    fetched_at: float
    body_path: Path


class RobotsRules:
    """Very small robots.txt parser.

    Scope: supports User-agent: * blocks with Allow/Disallow prefix matching.
    This is intentionally conservative.
    """

    def __init__(self, raw_text: str) -> None:
        self._allow: list[str] = []
        self._disallow: list[str] = []

        active_for_star = False
        for line in raw_text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "#" in line:
                line = line.split("#", 1)[0].strip()
            if not line:
                continue

            key, _, value = line.partition(":")
            key = key.strip().lower()
            value = value.strip()

            if key == "user-agent":
                active_for_star = value == "*"
                continue

            if not active_for_star:
                continue

            if key == "disallow":
                if value:
                    self._disallow.append(value)
            elif key == "allow":
                if value:
                    self._allow.append(value)

        # Longest prefix wins.
        self._allow.sort(key=len, reverse=True)
        self._disallow.sort(key=len, reverse=True)

    def can_fetch(self, url: str) -> bool:
        path = urlparse(url).path or "/"

        # Allow overrides disallow if it matches with longer/equal prefix.
        for allow_prefix in self._allow:
            if path.startswith(allow_prefix):
                return True
        for disallow_prefix in self._disallow:
            if path.startswith(disallow_prefix):
                return False
        return True


class Crawler:
    def __init__(
        self,
        *,
        out_dir: Path,
        session: requests.Session,
        allow_host_suffixes: list[str],
        follow_offsite: bool,
        api_key: str | None,
        api_key_env_name: str,
        api_key_header: str,
        api_key_send_hosts: set[str],
        per_host_delay_s: float,
        timeout_s: int,
        max_retries: int,
        backoff_base_s: float,
        max_pages: int,
        max_depth: int,
        refresh_cache: bool,
        respect_robots: bool,
    ) -> None:
        self.out_dir = out_dir
        self.session = session

        self.allow_host_suffixes = [s.lower().lstrip(".") for s in allow_host_suffixes]
        self.follow_offsite = follow_offsite

        self.api_key = api_key
        self.api_key_env_name = api_key_env_name
        self.api_key_header = api_key_header
        self.api_key_send_hosts = {h.lower() for h in api_key_send_hosts}

        self.per_host_delay_s = per_host_delay_s
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.backoff_base_s = backoff_base_s
        self.max_pages = max_pages
        self.max_depth = max_depth
        self.refresh_cache = refresh_cache
        self.respect_robots = respect_robots

        self.cache_dir = self.out_dir / ".cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.raw_dir = self.out_dir / "raw"
        self.pages_dir = self.out_dir / "pages"
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.pages_dir.mkdir(parents=True, exist_ok=True)

        self.state_dir = self.out_dir / ".state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.seen_path = self.state_dir / "seen_urls.txt"
        self.queue_path = self.state_dir / "queue_urls.txt"
        self.robots_dir = self.state_dir / "robots"
        self.robots_dir.mkdir(parents=True, exist_ok=True)

        self.manifest_jsonl = self.out_dir / "manifest.jsonl"
        self.manifest_summary = self.out_dir / "manifest.json"

        self.seen: set[str] = set()
        if self.seen_path.exists():
            seen_lines = self.seen_path.read_text(encoding="utf-8").splitlines()
            self.seen = {line.strip() for line in seen_lines if line.strip()}

        self.robots_cache: dict[str, RobotsRules] = {}
        self.host_last_request: dict[str, float] = {}

        self.stats: Counter[str] = Counter()

    def _host_allowed(self, host: str) -> bool:
        host = host.lower()
        if self.follow_offsite:
            return True
        if not host:
            return False
        for suffix in self.allow_host_suffixes:
            if host == suffix or host.endswith("." + suffix):
                return True
        return False

    def _enqueue(
        self,
        queue: deque[tuple[str, int, str]],
        url: str,
        depth: int,
        discovered_from: str,
    ) -> None:
        url = _normalize_url(url)
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return
        if not self._host_allowed(parsed.hostname or ""):
            return
        if url in self.seen:
            return
        self.seen.add(url)
        self.seen_path.parent.mkdir(parents=True, exist_ok=True)
        with self.seen_path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(url + "\n")
        queue.append((url, depth, discovered_from))

    def _cache_paths(self, url: str) -> tuple[Path, Path]:
        base = _url_hash(url)
        return self.cache_dir / f"{base}.bin", self.cache_dir / f"{base}.json"

    def _load_cached_meta(self, meta_path: Path) -> dict | None:
        if not meta_path.exists():
            return None
        try:
            return json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None

    def _throttle(self, host: str) -> None:
        host = host.lower()
        now = time.time()
        last = self.host_last_request.get(host)
        if last is not None:
            elapsed = now - last
            if elapsed < self.per_host_delay_s:
                time.sleep(self.per_host_delay_s - elapsed)
        self.host_last_request[host] = time.time()

    def _fetch_robots(self, host: str) -> RobotsRules:
        host = host.lower()
        if host in self.robots_cache:
            return self.robots_cache[host]

        robots_path = self.robots_dir / f"{_safe_filename_piece(host, max_len=120)}.txt"
        if robots_path.exists():
            rules = RobotsRules(
                robots_path.read_text(encoding="utf-8", errors="replace")
            )
            self.robots_cache[host] = rules
            return rules

        robots_url = f"https://{host}/robots.txt"
        try:
            self._throttle(host)
            resp = self.session.get(robots_url, timeout=self.timeout_s)
            text = resp.text if resp.ok else ""
        except requests.RequestException:
            text = ""
        robots_path.write_text(text, encoding="utf-8", newline="\n")
        rules = RobotsRules(text)
        self.robots_cache[host] = rules
        return rules

    def _fetch(self, url: str) -> CachedResponse:
        normalized = _normalize_url(url)
        body_path, meta_path = self._cache_paths(normalized)
        cached_meta = self._load_cached_meta(meta_path)

        headers: dict[str, str] = {
            "User-Agent": ("extract-ocr/ingest_data_uspto_gov (+https://github.com/)")
        }

        # Conditional GET on refresh.
        if self.refresh_cache and cached_meta:
            etag = cached_meta.get("etag")
            last_modified = cached_meta.get("last_modified")
            if etag:
                headers["If-None-Match"] = etag
            if last_modified:
                headers["If-Modified-Since"] = last_modified

        host = (urlparse(normalized).hostname or "").lower()

        # Attach API key only to explicit hosts.
        if self.api_key and host in self.api_key_send_hosts:
            headers[self.api_key_header] = self.api_key

        # If not refreshing and body exists, treat as cache hit.
        if body_path.exists() and meta_path.exists() and not self.refresh_cache:
            meta = cached_meta or {}
            return CachedResponse(
                url=normalized,
                final_url=str(meta.get("final_url") or normalized),
                status_code=int(meta.get("status_code") or 200),
                headers=dict(meta.get("headers") or {}),
                fetched_at=float(meta.get("fetched_at") or 0.0),
                body_path=body_path,
            )

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                self._throttle(host)
                resp = self.session.get(
                    normalized,
                    timeout=self.timeout_s,
                    headers=headers,
                    allow_redirects=True,
                )

                if resp.status_code == 304 and body_path.exists():
                    meta = cached_meta or {}
                    return CachedResponse(
                        url=normalized,
                        final_url=str(meta.get("final_url") or str(resp.url)),
                        status_code=304,
                        headers=dict(meta.get("headers") or {}),
                        fetched_at=float(meta.get("fetched_at") or 0.0),
                        body_path=body_path,
                    )

                if resp.status_code in TRANSIENT_HTTP_STATUSES:
                    retry_after_s = _retry_after_seconds(dict(resp.headers))
                    if attempt < self.max_retries:
                        wait_s = (
                            retry_after_s
                            if retry_after_s is not None
                            else self.backoff_base_s * (2**attempt)
                        )
                        time.sleep(wait_s)
                        continue

                # Cache non-2xx responses; raise for persistent failures.
                body_path.write_bytes(resp.content)
                meta = {
                    "url": normalized,
                    "final_url": str(resp.url),
                    "status_code": resp.status_code,
                    "fetched_at": time.time(),
                    "etag": resp.headers.get("ETag"),
                    "last_modified": resp.headers.get("Last-Modified"),
                    "headers": {
                        "content-type": resp.headers.get("Content-Type", ""),
                        "content-length": resp.headers.get("Content-Length", ""),
                    },
                }
                meta_path.write_text(
                    json.dumps(meta, indent=2),
                    encoding="utf-8",
                )

                resp.raise_for_status()
                return CachedResponse(
                    url=normalized,
                    final_url=str(resp.url),
                    status_code=resp.status_code,
                    headers=dict(meta["headers"]),
                    fetched_at=float(meta["fetched_at"]),
                    body_path=body_path,
                )
            except (requests.RequestException, OSError, ValueError) as e:
                last_error = e
                if attempt >= self.max_retries:
                    break
                time.sleep(self.backoff_base_s * (2**attempt))

        raise RuntimeError(f"Failed to fetch {normalized}: {last_error}")

    def _is_html(self, url: str, headers: dict[str, str]) -> bool:
        ct = (headers.get("content-type") or "").lower()
        if "text/html" in ct or "application/xhtml+xml" in ct:
            return True
        path = urlparse(url).path.lower()
        return path.endswith(".html") or path.endswith(".htm") or path.endswith("/")

    def _is_xml_sitemap(self, url: str, headers: dict[str, str]) -> bool:
        ct = (headers.get("content-type") or "").lower()
        if "xml" in ct:
            return True
        return urlparse(url).path.lower().endswith(".xml")

    def _append_manifest_item(self, item: dict) -> None:
        with self.manifest_jsonl.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")

    def _export_raw(self, cached: CachedResponse) -> Path:
        content_type = cached.headers.get("content-type")
        ext = _guess_extension(cached.final_url, content_type)
        subdir = "other"
        if ext in {".html", ".htm"}:
            subdir = "html"
        elif ext == ".pdf":
            subdir = "pdf"
        elif ext == ".json":
            subdir = "json"
        elif ext in {".xml"}:
            subdir = "xml"
        elif ext in {".yaml", ".yml"}:
            subdir = "yaml"
        elif ext in {".txt"}:
            subdir = "txt"

        out = self.raw_dir / subdir
        out.mkdir(parents=True, exist_ok=True)
        target = out / f"{_url_hash(cached.url)}{ext}"
        if not target.exists():
            target.write_bytes(cached.body_path.read_bytes())
        return target

    def _html_to_markdown(self, html: str, base_url: str) -> tuple[str, str]:
        soup = BeautifulSoup(html, "html.parser")

        # Remove irrelevant bits.
        for tag_name in ["script", "style", "noscript"]:
            for t in soup.find_all(tag_name):
                t.decompose()

        title = "Untitled"
        h1 = soup.find("h1")
        if h1 is not None and h1.get_text(strip=True):
            title = h1.get_text(" ", strip=True)
        elif soup.title and soup.title.get_text(strip=True):
            title = soup.title.get_text(" ", strip=True)

        main_node = None
        for selector in [
            "main",
            "article",
            "#topic-content",
            "#topic",
            "#rh-topic",
            "div[role='main']",
            "div[role='document']",
        ]:
            node = soup.select_one(selector)
            if node and node.get_text(strip=True):
                main_node = node
                break
        if main_node is None:
            main_node = soup.body or soup

        markdown = md(str(main_node), heading_style="ATX")
        markdown = markdown.strip() + "\n\n" + f"Source: {base_url}\n"
        return title, markdown

    def _extract_links(self, html: str, base_url: str) -> Iterable[str]:
        soup = BeautifulSoup(html, "html.parser")
        # Discover URLs from common URL-bearing elements.
        # Keep this conservative: we only follow explicit URL-like attributes.
        selector = (
            "a[href], link[href], img[src], script[src], iframe[src], "
            "source[src], video[src], audio[src], embed[src], object[data], "
            "form[action]"
        )
        for tag in soup.select(selector):
            if tag.name in {"a", "link"}:
                attr = "href"
            elif tag.name == "object":
                attr = "data"
            elif tag.name == "form":
                attr = "action"
            else:
                attr = "src"

            raw_value = tag.get(attr)
            if raw_value is None:
                continue
            if isinstance(raw_value, list):
                raw_value = raw_value[0] if raw_value else ""
            raw = str(raw_value).strip()
            if not raw:
                continue
            if raw.startswith("javascript:") or raw.startswith("mailto:"):
                continue
            abs_url = urljoin(base_url, raw)
            yield _normalize_url(abs_url)

        # Meta refresh redirects (e.g. content="0; url=/path").
        for meta in soup.select("meta[http-equiv][content]"):
            http_equiv_raw = meta.get("http-equiv")
            if isinstance(http_equiv_raw, list):
                http_equiv_raw = http_equiv_raw[0] if http_equiv_raw else ""
            http_equiv = str(http_equiv_raw or "").strip().lower()
            if http_equiv != "refresh":
                continue
            content = str(meta.get("content") or "")
            # Look for "url=..."; tolerate casing and whitespace.
            m = re.search(r"\burl\s*=\s*([^;]+)", content, flags=re.IGNORECASE)
            if not m:
                continue
            raw = m.group(1).strip().strip('"').strip("'")
            if not raw:
                continue
            abs_url = urljoin(base_url, raw)
            yield _normalize_url(abs_url)

    def _discover_sitemaps_from_robots(self, host: str) -> list[str]:
        host = host.lower()
        robots_path = self.robots_dir / f"{_safe_filename_piece(host, max_len=120)}.txt"
        if not robots_path.exists():
            return []
        sitemaps: list[str] = []
        for line in robots_path.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines():
            if line.lower().startswith("sitemap:"):
                _, _, value = line.partition(":")
                url = value.strip()
                if url:
                    sitemaps.append(_normalize_url(url))
        return sitemaps

    def _parse_sitemap_urls(self, xml_bytes: bytes) -> list[str]:
        # Minimal parsing via BeautifulSoup.
        soup = BeautifulSoup(xml_bytes, "xml")
        urls: list[str] = []
        for loc in soup.find_all("loc"):
            if loc and loc.get_text(strip=True):
                urls.append(_normalize_url(loc.get_text(strip=True)))
        return urls

    def _persist_queue(self, queue: deque[tuple[str, int, str]]) -> None:
        tmp = self.queue_path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8", newline="\n") as fh:
            for url, depth, discovered_from in queue:
                fh.write(f"{url}\t{depth}\t{discovered_from}\n")
        tmp.replace(self.queue_path)

    def load_or_seed_queue(self, seeds: list[str]) -> deque[tuple[str, int, str]]:
        queue: deque[tuple[str, int, str]] = deque()
        if self.queue_path.exists():
            for line in self.queue_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                parts = line.split("\t")
                url = parts[0].strip()
                depth = int(parts[1]) if len(parts) > 1 else 0
                discovered_from = parts[2].strip() if len(parts) > 2 else "resume"
                queue.append((url, depth, discovered_from))
            return queue

        for seed in seeds:
            self._enqueue(queue, seed, 0, "seed")

        # Robots + sitemap seeding for data.uspto.gov.
        for seed in seeds:
            host = (urlparse(seed).hostname or "").lower()
            if not host:
                continue
            if not self._host_allowed(host):
                continue
            if self.respect_robots:
                self._fetch_robots(host)
            for sm in self._discover_sitemaps_from_robots(host):
                self._enqueue(queue, sm, 0, "robots:sitemap")

            # Common default sitemap.
            self._enqueue(
                queue,
                f"https://{host}/sitemap.xml",
                0,
                "default:sitemap",
            )

        self._persist_queue(queue)
        return queue

    def ingest_local_seed_pdf(self, path: Path, source_url: str) -> None:
        if not path.exists():
            return
        target_dir = self.raw_dir / "pdf"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / path.name
        if not target.exists():
            target.write_bytes(path.read_bytes())

        self._append_manifest_item(
            {
                "kind": "local_seed",
                "url": _normalize_url(source_url),
                "stored_at": _utc_iso(),
                "paths": {"pdf": str(target.relative_to(self.out_dir))},
            }
        )

    def crawl(self, *, seeds: list[str]) -> None:
        queue = self.load_or_seed_queue(seeds)

        # Include user-provided mapping PDF if present.
        local_pdf = Path.cwd() / "PEDS-to-ODP-API-Mapping.pdf"
        self.ingest_local_seed_pdf(
            local_pdf,
            (
                "https://data.uspto.gov/documents/documents/"
                "PEDS-to-ODP-API-Mapping.pdf"
            ),
        )

        processed = 0
        persist_every = 25

        while queue:
            if self.max_pages > 0 and processed >= self.max_pages:
                break

            url, depth, discovered_from = queue.popleft()
            if self.max_depth > 0 and depth > self.max_depth:
                self.stats["skipped_max_depth"] += 1
                continue

            host = (urlparse(url).hostname or "").lower()
            if self.respect_robots and host:
                rules = self._fetch_robots(host)
                if not rules.can_fetch(url):
                    self.stats["robots_blocked"] += 1
                    self._append_manifest_item(
                        {
                            "kind": "blocked",
                            "url": url,
                            "discovered_from": discovered_from,
                            "depth": depth,
                            "reason": "robots.txt",
                            "stored_at": _utc_iso(),
                        }
                    )
                    continue

            try:
                cached = self._fetch(url)
                processed += 1
                self.stats["fetched"] += 1

                raw_path = self._export_raw(cached)

                item: dict = {
                    "kind": "fetched",
                    "url": cached.url,
                    "final_url": cached.final_url,
                    "status_code": cached.status_code,
                    "content_type": cached.headers.get("content-type", ""),
                    "discovered_from": discovered_from,
                    "depth": depth,
                    "stored_at": _utc_iso(),
                    "paths": {"raw": str(raw_path.relative_to(self.out_dir))},
                }

                # Sitemap discovery.
                if self._is_xml_sitemap(cached.final_url, cached.headers):
                    try:
                        urls = self._parse_sitemap_urls(cached.body_path.read_bytes())
                        self.stats["sitemap_urls"] += len(urls)
                        base_url = cached.final_url
                        for u in urls:
                            self._enqueue(queue, u, depth + 1, base_url)
                    except (OSError, ValueError):
                        self.stats["sitemap_parse_errors"] += 1

                # HTML discovery + optional markdown export.
                if self._is_html(cached.final_url, cached.headers):
                    try:
                        html_text = cached.body_path.read_text(
                            encoding="utf-8", errors="replace"
                        )
                        title, markdown = self._html_to_markdown(
                            html_text, cached.final_url
                        )
                        safe_title = _safe_filename_piece(title)
                        url_key = _url_hash(cached.url)
                        md_name = f"{safe_title}--{url_key}.md"
                        md_path = self.pages_dir / md_name
                        if not md_path.exists():
                            md_path.write_text(
                                markdown,
                                encoding="utf-8",
                                newline="\n",
                            )
                        item["title"] = title
                        item["paths"]["markdown"] = str(
                            md_path.relative_to(self.out_dir)
                        )

                        base_url = cached.final_url
                        for link in self._extract_links(html_text, base_url):
                            self._enqueue(queue, link, depth + 1, base_url)
                        self.stats["html_pages"] += 1
                    except (OSError, UnicodeError, ValueError):
                        self.stats["html_parse_errors"] += 1

                self._append_manifest_item(item)

            except (
                requests.RequestException,
                RuntimeError,
                OSError,
                UnicodeError,
                ValueError,
            ) as e:
                self.stats["errors"] += 1
                self._append_manifest_item(
                    {
                        "kind": "error",
                        "url": url,
                        "discovered_from": discovered_from,
                        "depth": depth,
                        "stored_at": _utc_iso(),
                        "error": str(e),
                    }
                )

            if processed % persist_every == 0:
                self._persist_queue(queue)

        # Final queue persistence.
        self._persist_queue(queue)

        summary = {
            "generated_at": _utc_iso(),
            "seeds": seeds,
            "out_dir": str(self.out_dir),
            "respect_robots": self.respect_robots,
            "follow_offsite": self.follow_offsite,
            "allow_host_suffixes": self.allow_host_suffixes,
            "api_key_env": self.api_key_env_name if self.api_key else None,
            "stats": dict(self.stats),
        }
        self.manifest_summary.write_text(
            json.dumps(summary, indent=2), encoding="utf-8"
        )


def _split_csv(value: str) -> list[str]:
    items = [v.strip() for v in value.split(",")]
    return [v for v in items if v]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ingest data.uspto.gov for offline use"
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("docs/USPTO ODP/export/data-uspto-gov"),
        help=("Output directory " "(default: docs/USPTO ODP/export/data-uspto-gov)"),
    )
    parser.add_argument(
        "--seed",
        action="append",
        default=["https://data.uspto.gov/apis/"],
        help="Seed URL (repeatable). Default: https://data.uspto.gov/apis/",
    )
    parser.add_argument(
        "--allow-host-suffix",
        action="append",
        default=["uspto.gov"],
        help="Allowed host suffix (repeatable). Default: uspto.gov",
    )
    parser.add_argument(
        "--follow-offsite",
        action="store_true",
        help="Allow following links to any host (ignores allow-host-suffix).",
    )
    parser.add_argument(
        "--per-host-delay",
        type=float,
        default=0.5,
        help="Delay between requests per host",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=45,
        help="HTTP timeout seconds",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=4,
        help="Max retries for transient failures",
    )
    parser.add_argument(
        "--backoff-base", type=float, default=1.0, help="Backoff base seconds"
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=500,
        help="Max fetched pages (0=unlimited)",
    )
    parser.add_argument(
        "--max-depth", type=int, default=8, help="Max link depth (0=unlimited)"
    )
    parser.add_argument(
        "--refresh-cache",
        action="store_true",
        help="Revalidate cached items via conditional GET",
    )
    parser.add_argument(
        "--no-robots",
        action="store_true",
        help="Do not fetch/respect robots.txt (NOT recommended).",
    )

    parser.add_argument(
        "--api-key-env",
        default="USPTO_ODP_API_KEY",
        help=(
            "Environment variable containing API key " "(default: USPTO_ODP_API_KEY)"
        ),
    )
    parser.add_argument(
        "--api-key-header",
        default="X-API-KEY",
        help="Header name to send API key in (default: X-API-KEY)",
    )
    parser.add_argument(
        "--api-key-host",
        action="append",
        default=["api.uspto.gov"],
        help="Host(s) to send API key to (repeatable). Default: api.uspto.gov",
    )

    args = parser.parse_args()

    out_dir: Path = args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    api_key = os.getenv(args.api_key_env) or None

    session = requests.Session()
    session.headers.update({"Accept": "*/*"})

    crawler = Crawler(
        out_dir=out_dir,
        session=session,
        allow_host_suffixes=list(args.allow_host_suffix),
        follow_offsite=bool(args.follow_offsite),
        api_key=api_key,
        api_key_env_name=str(args.api_key_env),
        api_key_header=str(args.api_key_header),
        api_key_send_hosts={h.lower() for h in args.api_key_host},
        per_host_delay_s=float(args.per_host_delay),
        timeout_s=int(args.timeout),
        max_retries=int(args.max_retries),
        backoff_base_s=float(args.backoff_base),
        max_pages=int(args.max_pages),
        max_depth=int(args.max_depth),
        refresh_cache=bool(args.refresh_cache),
        respect_robots=not bool(args.no_robots),
    )

    # Normalize seeds.
    seeds = [_normalize_url(s) for s in args.seed]
    crawler.crawl(seeds=seeds)


if __name__ == "__main__":
    main()
