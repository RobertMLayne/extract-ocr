"""Export EndNote 2025 (EndNote 25) Windows web help into local Markdown.

This script is designed to *download and consolidate* documentation pages for
offline use.
It does not print page bodies to stdout by default.

Typical usage:
    C:/Dev/Projects/extract-ocr/.venv312/Scripts/python.exe \
        scripts/export_endnote25_windows.py \
    --leftpanel "docs/EndNote 25/endnote25_windows_leftpanel.html" \
    --out "docs/EndNote 25/export/endnote25-windows"

If you don't have the left panel HTML, you can still export a single page:
    ... export_endnote25_windows.py --url \
        https://docs.endnote.com/docs/endnote/2025/v1/windows/en/content/00endnote_libraries/00endnote_libraries_and_references.htm

Notes:
- Respects robots/terms is your responsibility.
- EndNote documentation content is likely copyrighted; this tool saves it
    locally.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import ParseResult, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from markdownify import markdownify as md
from tqdm import tqdm  # type: ignore[import-untyped]


def _load_inspect_export() -> Callable[..., Any]:
    """Dynamically import `inspect_export` from the src-layout package.

    This script is typically run as `python scripts/...py` (without installing
    the package), so `src/` is not automatically on `sys.path`.
    """

    repo_root = Path(__file__).resolve().parents[1]
    src_dir = repo_root / "src"
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))

    mod = importlib.import_module("extract_ocr.export_inspect")
    return getattr(mod, "inspect_export")


DEFAULT_SEED_URL = (
    "https://docs.endnote.com/docs/endnote/2025/v1/windows/en/"
    "content/00endnote_libraries/00endnote_libraries_and_references.htm"
)


@dataclass(frozen=True)
class Page:
    url: str
    title: str
    markdown: str


def reset_dir(path: Path) -> None:
    """Replace an output directory to avoid stale content."""
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def _normalize_url(raw_url: str) -> str:
    """Normalize URLs for de-duplication.

    - Strips fragments.
    - Drops trivial tracking query params like `agt=index`.
    """

    parsed: ParseResult = urlparse(raw_url)
    query = parsed.query
    if query.strip().lower() == "agt=index":
        query = ""
    parsed = parsed._replace(fragment="", query=query)
    return urlunparse(parsed)


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]


def _cache_paths(cache_dir: Path, url: str) -> tuple[Path, Path]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    base = _url_hash(url)
    return cache_dir / f"{base}.html", cache_dir / f"{base}.json"


def _retry_after_seconds(response: requests.Response) -> float | None:
    retry_after = response.headers.get("Retry-After")
    if not retry_after:
        return None
    try:
        return float(retry_after)
    except ValueError:
        return None


def _decode_response_html(response: requests.Response) -> str:
    """Decode HTML bytes to text.

    EndNote help pages are typically UTF-8, but some responses may be labeled
    or
    inferred incorrectly by the client, causing mojibake
    (e.g. "Â©", "â€”").
    """

    content = response.content
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        encoding = response.encoding or response.apparent_encoding or "utf-8"
        return content.decode(encoding, errors="replace")


def fetch_page(
    session: requests.Session,
    url: str,
    *,
    timeout_s: int = 45,
    max_retries: int = 4,
    backoff_base_s: float = 1.0,
    cache_dir: Path | None = None,
    refresh_cache: bool = False,
) -> str:
    """Fetch a page with basic retry/backoff and optional on-disk caching.

    Caching behavior:
    - If cached and not refresh_cache: returns cached.
    - If refresh_cache: attempts conditional GET using ETag/Last-Modified.
    """

    normalized = _normalize_url(url)

    cached_html_path: Path | None = None
    cached_meta_path: Path | None = None
    cached_meta: dict | None = None
    if cache_dir is not None:
        cached_html_path, cached_meta_path = _cache_paths(
            cache_dir,
            normalized,
        )
        if cached_meta_path.exists():
            try:
                cached_meta_text = cached_meta_path.read_text(encoding="utf-8")
                cached_meta = json.loads(cached_meta_text)
            except (OSError, ValueError):
                cached_meta = None

        if cached_html_path.exists() and not refresh_cache:
            return cached_html_path.read_text(
                encoding="utf-8",
                errors="replace",
            )

    headers = {}
    if refresh_cache and cached_meta:
        etag = cached_meta.get("etag")
        last_modified = cached_meta.get("last_modified")
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            response = session.get(
                normalized,
                timeout=timeout_s,
                headers=headers,
            )
            if (
                response.status_code == 304
                and cached_html_path
                and cached_html_path.exists()
            ):
                return cached_html_path.read_text(
                    encoding="utf-8",
                    errors="replace",
                )

            # Retry transient failures.
            if response.status_code in {429, 500, 502, 503, 504}:
                retry_after_s = _retry_after_seconds(response)
                if attempt < max_retries:
                    wait_s = (
                        retry_after_s
                        if retry_after_s is not None
                        else backoff_base_s * (2**attempt)
                    )
                    time.sleep(wait_s)
                    continue

            response.raise_for_status()
            html = _decode_response_html(response)

            if cache_dir is not None and cached_html_path and cached_meta_path:
                cached_html_path.write_text(
                    html,
                    encoding="utf-8",
                    newline="\n",
                )
                meta = {
                    "url": normalized,
                    "fetched_at": time.time(),
                    "etag": response.headers.get("ETag"),
                    "last_modified": response.headers.get("Last-Modified"),
                }
                cached_meta_path.write_text(
                    json.dumps(meta, indent=2), encoding="utf-8"
                )
            return html
        except requests.RequestException as e:
            last_error = e
            if attempt >= max_retries:
                break
            time.sleep(backoff_base_s * (2**attempt))

    raise RuntimeError(f"Failed to fetch {normalized}: {last_error}")


def extract_hrefs_from_leftpanel_html(leftpanel_html: str) -> list[str]:
    soup = BeautifulSoup(leftpanel_html, "html.parser")
    hrefs: list[str] = []
    for a in soup.select("a[href]"):
        href_val = a.get("href")
        if href_val is None:
            continue
        if isinstance(href_val, list):
            href_val = " ".join(str(v) for v in href_val)
        href = str(href_val).strip()
        if not href:
            continue
        # Only TOC/Index links that point to a page.
        if not re.search(r"\.(?:htm|html)(?:\?|$)", href, flags=re.IGNORECASE):
            continue
        hrefs.append(href)
    return hrefs


def build_absolute_url_list(hrefs: Iterable[str], seed_url: str) -> list[str]:
    """Resolve potentially relative hrefs against the seed URL."""
    seen: set[str] = set()
    ordered: list[str] = []

    for href in hrefs:
        abs_url = urljoin(seed_url, href)
        abs_url = _normalize_url(abs_url)
        if abs_url in seen:
            continue
        seen.add(abs_url)
        ordered.append(abs_url)

    return ordered


def _clean_soup_inplace(soup: BeautifulSoup) -> None:
    # Remove irrelevant bits.
    for tag_name in ["script", "style", "noscript"]:
        for t in soup.find_all(tag_name):
            t.decompose()


def _pick_main_content(soup: BeautifulSoup):
    """Best-effort main content selection for RoboHelp-like pages."""

    # Common candidates.
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
            return node

    # Heuristic: largest div by text length.
    best = None
    best_len = 0
    for div in soup.find_all("div"):
        text_len = len(div.get_text(" ", strip=True))
        if text_len > best_len:
            best = div
            best_len = text_len
    return best or soup.body or soup


def _extract_title(soup: BeautifulSoup) -> str:
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return h1.get_text(" ", strip=True)
    if soup.title and soup.title.get_text(strip=True):
        return soup.title.get_text(" ", strip=True)
    return "Untitled"


def parse_page_to_markdown(html: str, url: str) -> Page:
    soup = BeautifulSoup(html, "html.parser")
    _clean_soup_inplace(soup)

    title = _extract_title(soup)
    main_content = _pick_main_content(soup)

    # Convert selected HTML subtree to Markdown.
    # Use heading_style='ATX' for stable output.
    markdown = md(str(main_content), heading_style="ATX")

    # Light normalization.
    markdown = markdown.replace("\r\n", "\n")
    markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip() + "\n"

    # Add a source marker at top.
    header = f"# {title}\n\nSource: {url}\n\n"
    return Page(url=url, title=title, markdown=header + markdown)


def _safe_slug(text: str, max_len: int = 80) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    if not text:
        text = "page"
    return text[:max_len].rstrip("-")


_MD_LINK_RE = re.compile(r"(!?\[[^\]]*\])\(([^)]+)\)")


def _rewrite_markdown_links(
    markdown: str, page_url: str, url_to_relpath: dict[str, str]
) -> str:
    """Rewrite links pointing to other EndNote pages to local exported files.

    External links are preserved.
    """

    def repl(match: re.Match) -> str:
        label = match.group(1)
        raw_target = match.group(2).strip()
        # Strip common Markdown angle-bracket wrapping.
        target = raw_target
        if target.startswith("<") and target.endswith(">"):
            target = target[1:-1].strip()

        # Preserve mailto and fragments.
        if target.startswith("mailto:"):
            return match.group(0)

        parsed = urlparse(target)
        fragment = parsed.fragment

        # Resolve relative URLs.
        resolved = urljoin(page_url, target)
        resolved_norm = _normalize_url(resolved)
        local = url_to_relpath.get(resolved_norm)
        if not local:
            return match.group(0)

        new_target = local
        if fragment:
            new_target = f"{new_target}#{fragment}"
        return f"{label}({new_target})"

    return _MD_LINK_RE.sub(repl, markdown)


def export(
    urls: list[str],
    out_dir: Path,
    *,
    delay_s: float = 0.2,
    clean: bool = False,
    cache_dir: Path | None = None,
    refresh_cache: bool = False,
    timeout_s: int = 45,
    max_retries: int = 4,
    rewrite_links: bool = True,
) -> dict:
    if clean:
        reset_dir(out_dir)
    else:
        out_dir.mkdir(parents=True, exist_ok=True)

    pages_dir = out_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    if cache_dir is None:
        cache_dir = out_dir / ".cache"

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "extract-ocr-endnote-export/1.0 (local)",
            "Accept": "text/html,application/xhtml+xml",
        }
    )

    index: list[dict] = []
    consolidated_path = out_dir / "endnote25-windows.md"

    with consolidated_path.open(
        "w",
        encoding="utf-8",
        newline="\n",
    ) as consolidated:
        consolidated.write("# EndNote 2025 (EndNote 25) — Windows\n")
        consolidated.write("\n")
        consolidated.write(
            "This file was generated locally by "
            "scripts/export_endnote25_windows.py.\n"
        )
        consolidated.write("\n")
        consolidated.write("## Table of Contents\n\n")

        pages: list[Page] = []
        page_files: dict[str, Path] = {}

        for url in tqdm(urls, desc="Fetching pages", unit="page"):
            try:
                html = fetch_page(
                    session,
                    url,
                    timeout_s=timeout_s,
                    max_retries=max_retries,
                    cache_dir=cache_dir,
                    refresh_cache=refresh_cache,
                )
                page = parse_page_to_markdown(html, url)

                slug = _safe_slug(page.title)
                filename = f"{slug}--{_url_hash(url)}.md"
                page_path = pages_dir / filename

                pages.append(page)
                page_files[_normalize_url(page.url)] = page_path

                rel = page_path.relative_to(out_dir).as_posix()
                index.append(
                    {
                        "title": page.title,
                        "url": _normalize_url(page.url),
                        "file": rel,
                    }
                )
            except (
                OSError,
                ValueError,
                RuntimeError,
                requests.RequestException,
            ) as e:
                index.append(
                    {
                        "title": None,
                        "url": _normalize_url(url),
                        "file": None,
                        "error": str(e),
                    }
                )
            finally:
                if delay_s > 0:
                    time.sleep(delay_s)

        url_to_relpath = {
            url: path.relative_to(out_dir).as_posix()
            for url, path in page_files.items()
        }

        # Write pages after we have a full URL->file map
        # (enables link rewriting).
        for page in pages:
            page_path = page_files[_normalize_url(page.url)]
            body = page.markdown
            if rewrite_links:
                body = _rewrite_markdown_links(
                    body, page_url=page.url, url_to_relpath=url_to_relpath
                )
            page_path.write_text(body, encoding="utf-8", newline="\n")

        # TOC
        for item in index:
            if item.get("file") and item.get("title"):
                consolidated.write(f"- [{item['title']}]({item['file']})\n")
        consolidated.write("\n")

        # Body
        for page in pages:
            consolidated.write("\n---\n\n")
            consolidated.write(page.markdown)

    manifest_path = out_dir / "manifest.json"
    manifest = {
        "seed_url": DEFAULT_SEED_URL,
        "count_requested": len(urls),
        "count_exported": sum(1 for i in index if i.get("file")),
        "count_failed": sum(1 for i in index if i.get("error")),
        "items": index,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export EndNote 2025 Windows web documentation to Markdown"
    )
    parser.add_argument(
        "--leftpanel",
        type=str,
        help="Path to HTML containing the left panel element",
    )
    parser.add_argument(
        "--url", type=str, help="Single page URL to export (for debugging)"
    )
    parser.add_argument(
        "--seed",
        type=str,
        default=DEFAULT_SEED_URL,
        help="Seed URL for resolving relative hrefs",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=0,
        help="Limit number of pages (0 = no limit)",
    )
    parser.add_argument(
        "--out",
        type=str,
        default="docs/EndNote 25/export/endnote25-windows",
        help="Output directory",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.2,
        help="Delay between requests (seconds)",
    )
    parser.add_argument(
        "--timeout", type=int, default=45, help="Request timeout seconds"
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=4,
        help="Max retries for transient HTTP failures",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Delete output directory before writing",
    )
    parser.add_argument(
        "--refresh-cache",
        action="store_true",
        help="Refresh cached HTML using conditional GET",
    )
    parser.add_argument(
        "--no-rewrite-links",
        action="store_true",
        help="Do not rewrite internal links to local files",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Fail (non-zero) if referenced files are missing",
    )
    parser.add_argument(
        "--max-missing-sample",
        type=int,
        default=25,
        help="Max missing paths to print when --validate fails",
    )

    args = parser.parse_args()
    out_dir = Path(args.out)

    urls: list[str]
    if args.url:
        urls = [_normalize_url(args.url)]
    elif args.leftpanel:
        leftpanel_path = Path(args.leftpanel)
        if not leftpanel_path.exists():
            raise SystemExit(f"Leftpanel file not found: {leftpanel_path}")
        leftpanel_html = leftpanel_path.read_text(
            encoding="utf-8",
            errors="replace",
        )
        hrefs = extract_hrefs_from_leftpanel_html(leftpanel_html)
        urls = build_absolute_url_list(hrefs, seed_url=args.seed)
    else:
        raise SystemExit("Provide either --leftpanel or --url")

    if args.max_pages and args.max_pages > 0:
        urls = urls[: args.max_pages]

    manifest = export(
        urls,
        out_dir=out_dir,
        delay_s=args.delay,
        clean=args.clean,
        refresh_cache=args.refresh_cache,
        timeout_s=args.timeout,
        max_retries=args.retries,
        rewrite_links=not args.no_rewrite_links,
    )

    if bool(args.validate):
        try:
            inspect_export = _load_inspect_export()
            inspected: Any = inspect_export(
                export_dir=out_dir,
                max_missing_paths_sample=int(args.max_missing_sample),
            )
        except (OSError, ValueError) as e:
            print(str(e), file=sys.stderr)
            return 2

        if inspected.missing_files:
            print(
                f"inspect-export: missing_files={inspected.missing_files}",
                file=sys.stderr,
            )
            if inspected.missing_paths_sample:
                sample = "\n".join(inspected.missing_paths_sample)
                print(
                    "inspect-export: missing_paths_sample:\n" + sample,
                    file=sys.stderr,
                )
            return 4

    print(f"Wrote: {out_dir / 'endnote25-windows.md'}")
    print(f"Wrote: {out_dir / 'manifest.json'}")
    print(
        f"Requested={manifest['count_requested']} "
        f"Exported={manifest['count_exported']} "
        f"Failed={manifest['count_failed']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
