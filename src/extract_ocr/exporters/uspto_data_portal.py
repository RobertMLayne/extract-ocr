from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import requests

from ..crawl import CrawlConfig, Crawler
from ..http_client import HttpClient
from ..urls import UrlScope


@dataclass
class USPTODataPortalConfig:
    out_dir: Path
    max_pages: int = 200
    max_depth: int = 3
    per_host_delay_s: float = 0.5
    respect_robots: bool = True
    refresh_cache: bool = False


def run(config: USPTODataPortalConfig) -> dict:
    session = requests.Session()
    http = HttpClient(session)

    scope = UrlScope(allow_host_suffixes=("uspto.gov",), follow_offsite=False)
    crawl_cfg = CrawlConfig(
        out_dir=config.out_dir,
        scope=scope,
        max_pages=config.max_pages,
        max_depth=config.max_depth,
        per_host_delay_s=config.per_host_delay_s,
        respect_robots=config.respect_robots,
        refresh_cache=config.refresh_cache,
    )

    crawler = Crawler(http=http, config=crawl_cfg)

    seeds = [
        "https://data.uspto.gov/",
        "https://data.uspto.gov/apis/",
    ]

    return crawler.crawl(seeds)
