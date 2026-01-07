from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

from ..cache import cache_paths, read_cached, write_cached
from ..citations import CitationItem, write_bibtex, write_csl_json, write_ris
from ..convert.html_to_md import extract_title, html_to_markdown
from ..http_client import HttpClient
from ..manifest import ManifestWriter, relpath_posix, utc_iso
from ..urls import normalize_url, safe_filename_piece

DEFAULT_SEED_URL = (
    "https://docs.endnote.com/docs/endnote/2025/v1/windows/en/"
    "content/00endnote_libraries/00endnote_libraries_and_references.htm"
)


def extract_hrefs_from_leftpanel_html(leftpanel_html: str) -> list[str]:
    soup = BeautifulSoup(leftpanel_html, "html.parser")
    hrefs: list[str] = []
    for a in soup.select("a[href]"):
        href_val = a.get("href")
        if isinstance(href_val, list):
            href = href_val[0] if href_val else ""
        else:
            href = str(href_val or "")
        href = href.strip()
        if not href:
            continue
        if not re.search(r"\.(?:htm|html)(?:\?|$)", href, flags=re.IGNORECASE):
            continue
        hrefs.append(href)
    return hrefs


def build_absolute_url_list(hrefs: Iterable[str], seed_url: str) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for href in hrefs:
        abs_url = normalize_url(urljoin(seed_url, href))
        if abs_url in seen:
            continue
        seen.add(abs_url)
        ordered.append(abs_url)
    return ordered


@dataclass
class EndNoteExportConfig:
    out_dir: Path
    leftpanel_path: Path | None
    seed_url: str
    max_pages: int | None
    refresh_cache: bool
    emit_ris: bool
    emit_csl_json: bool
    emit_bibtex: bool


class EndNoteExporter:
    def __init__(
        self, *, session: requests.Session, config: EndNoteExportConfig
    ) -> None:
        self.cfg = config
        self.out_dir = self.cfg.out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)

        self.cache_dir = self.out_dir / ".cache"
        self.pages_dir = self.out_dir / "pages"
        self.citations_dir = self.out_dir / "citations"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.pages_dir.mkdir(parents=True, exist_ok=True)

        self.http = HttpClient(session)
        self.manifest = ManifestWriter(self.out_dir)
        self._citations: list[CitationItem] = []

    def _cache_key(self, url: str) -> str:
        # Keep consistent with other components.
        import hashlib

        return hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]

    def _fetch_html(self, url: str) -> str:
        url = normalize_url(url)
        cache_entry = cache_paths(self.cache_dir, key=self._cache_key(url))

        if (
            cache_entry.body_path.exists()
            and cache_entry.meta_path.exists()
            and not self.cfg.refresh_cache
        ):
            body, _meta = read_cached(cache_entry)
            if body is not None:
                return body.decode("utf-8", errors="replace")

        res = self.http.get(url)
        write_cached(cache_entry, res)
        return res.body.decode("utf-8", errors="replace")

    def export(self) -> dict:
        started_at = utc_iso()

        urls: list[str]
        if self.cfg.leftpanel_path is not None:
            leftpanel_html = self.cfg.leftpanel_path.read_text(
                encoding="utf-8", errors="replace"
            )
            hrefs = extract_hrefs_from_leftpanel_html(leftpanel_html)
            urls = build_absolute_url_list(hrefs, self.cfg.seed_url)
        else:
            urls = [normalize_url(self.cfg.seed_url)]

        if self.cfg.max_pages is not None:
            urls = urls[: self.cfg.max_pages]

        page_items: list[dict] = []

        for url in tqdm(urls, desc="EndNote export", unit="page"):
            try:
                html = self._fetch_html(url)
                title = extract_title(html)
                md_text = html_to_markdown(html, source_url=url)

                key = self._cache_key(url)
                md_name = f"{safe_filename_piece(title)}--{key}.md"
                md_path = self.pages_dir / md_name
                md_path.write_text(md_text, encoding="utf-8", newline="\n")

                rel = relpath_posix(md_path, self.out_dir)
                page_items.append({"url": url, "title": title, "path": rel})

                self._citations.append(
                    CitationItem(
                        title=title,
                        url=url,
                        accessed=started_at[:10],
                        local_path=rel,
                        publisher="Clarivate",
                    )
                )

                self.manifest.append(
                    {
                        "kind": "fetched",
                        "url": url,
                        "title": title,
                        "paths": {"page_md": rel},
                    }
                )
            except (
                OSError,
                UnicodeDecodeError,
                ValueError,
                requests.RequestException,
            ) as e:
                self.manifest.append(
                    {
                        "kind": "error",
                        "url": url,
                        "error": str(e),
                    }
                )

        # Write an index file (consolidated) with local links.
        index_md = ["# EndNote 25 Windows\n", "\n", "## Pages\n", "\n"]
        for it in page_items:
            index_md.append(f"- [{it['title']}]({it['path']})\n")

        index_path = self.out_dir / "endnote25-windows.md"
        index_path.write_text(
            "".join(index_md),
            encoding="utf-8",
            newline="\n",
        )

        if self.cfg.emit_ris:
            write_ris(
                self._citations,
                self.citations_dir / "endnote25-windows.ris",
            )
        if self.cfg.emit_csl_json:
            write_csl_json(
                self._citations,
                self.citations_dir / "endnote25-windows.csl.json",
            )
        if self.cfg.emit_bibtex:
            write_bibtex(
                self._citations,
                self.citations_dir / "endnote25-windows.bib",
            )

        summary = {
            "started_at": started_at,
            "finished_at": utc_iso(),
            "pages": len(page_items),
            "paths": {
                "index_md": relpath_posix(index_path, self.out_dir),
                "manifest_jsonl": "manifest.jsonl",
                "manifest_json": "manifest.json",
            },
        }
        self.manifest.write_summary(summary)

        # Also keep the old style manifest.json for compatibility.
        compat = {"pages": page_items}
        (self.out_dir / "manifest.json").write_text(
            json.dumps(compat, indent=2), encoding="utf-8"
        )

        return summary
