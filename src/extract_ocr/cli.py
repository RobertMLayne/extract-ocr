from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import requests

from .apis_report import collect_apis_report_data, write_apis_report
from .citations import write_bibtex, write_csl_json, write_ris
from .crawl import (
    CrawlConfig,
    Crawler,
    ensure_export_api_endpoint_variants,
    ensure_export_html_variants,
    ensure_export_non_html_variants,
    extract_links_from_html,
    is_waf_challenge,
)
from .export_inspect import inspect_export
from .exporters.endnote25_windows import EndNoteExportConfig, EndNoteExporter
from .exporters.uspto_data_portal import USPTODataPortalConfig
from .exporters.uspto_data_portal import run as run_uspto
from .http_client import HttpClient
from .urls import UrlScope


def _add_common_crawl_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--max-pages", type=int, default=200)
    p.add_argument("--max-depth", type=int, default=3)
    p.add_argument("--per-host-delay", type=float, default=0.5)
    p.add_argument("--timeout", type=int, default=45)
    p.add_argument("--refresh-cache", action="store_true")
    p.add_argument("--no-robots", action="store_true")
    p.add_argument("--follow-offsite", action="store_true")
    p.add_argument(
        "--allow-host-suffix",
        action="append",
        default=["uspto.gov"],
        help="Repeatable; e.g. --allow-host-suffix uspto.gov",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="extract_ocr")
    sub = parser.add_subparsers(dest="cmd", required=True)

    apis_p = sub.add_parser(
        "apis-report",
        help=(
            "Extract + dedupe https://data.uspto.gov/apis/... endpoints from "
            "an existing export directory and write a report file"
        ),
    )
    apis_p.add_argument(
        "--in",
        dest="in_dir",
        type=Path,
        required=True,
        help="Existing export directory containing manifest.jsonl/pages/raw",
    )
    apis_p.add_argument(
        "--out",
        dest="report_path",
        type=Path,
        default=None,
        help=(
            "Where to write the report. Defaults to " "<in>/apis_endpoints_report.md"
        ),
    )
    apis_p.add_argument(
        "--crawl",
        action="store_true",
        help=(
            "Also fetch discovered /apis/ endpoints over HTTP into --in, then "
            "regenerate the report"
        ),
    )
    apis_p.add_argument(
        "--crawl-max-pages",
        type=int,
        default=None,
        help="Max endpoint pages to fetch when --crawl is set (default: all)",
    )
    apis_p.add_argument(
        "--crawl-per-host-delay",
        type=float,
        default=0.5,
        help="Delay between requests to the same host when --crawl is set",
    )
    apis_p.add_argument(
        "--crawl-timeout",
        type=int,
        default=45,
        help="HTTP timeout seconds when --crawl is set",
    )
    apis_p.add_argument(
        "--crawl-refresh-cache",
        action="store_true",
        help="Ignore cached HTTP responses when --crawl is set",
    )
    apis_p.add_argument(
        "--crawl-no-robots",
        action="store_true",
        help="Do not respect robots.txt when --crawl is set",
    )
    apis_p.add_argument(
        "--crawl-follow-offsite",
        action="store_true",
        help="Allow offsite URLs when --crawl is set",
    )
    apis_p.add_argument(
        "--crawl-allow-host-suffix",
        action="append",
        default=["uspto.gov"],
        help="Repeatable; e.g. --crawl-allow-host-suffix uspto.gov",
    )

    crawl_p = sub.add_parser("crawl", help="Generic crawl/export")
    crawl_p.add_argument("--seed", action="append", required=True)
    _add_common_crawl_args(crawl_p)
    crawl_p.add_argument("--emit-ris", action="store_true")
    crawl_p.add_argument("--emit-csl-json", action="store_true")
    crawl_p.add_argument("--emit-bibtex", action="store_true")

    uspto_p = sub.add_parser(
        "uspto-data",
        help="Crawl data.uspto.gov with defaults",
    )
    _add_common_crawl_args(uspto_p)
    uspto_p.add_argument(
        "--validate",
        action="store_true",
        help="Fail (non-zero) if referenced files are missing",
    )
    uspto_p.add_argument(
        "--seed-html",
        type=Path,
        action="append",
        default=None,
        help=(
            "Path to a locally saved browser HTML snapshot. "
            "If provided, links are extracted from this HTML "
            "and used as crawl seed URLs."
        ),
    )
    uspto_p.add_argument(
        "--seed-html-dir",
        type=Path,
        default=None,
        help=(
            "Directory containing browser-saved HTML snapshots (*.html). "
            "All HTML files under this directory are ingested and used "
            "to bootstrap crawl seeds."
        ),
    )
    uspto_p.add_argument(
        "--seed-url",
        default=None,
        help=(
            "Base URL for resolving relative links in --seed-html. "
            "Defaults to https://data.uspto.gov/"
        ),
    )

    endnote_p = sub.add_parser(
        "endnote25",
        help="Export EndNote 25 Windows help",
    )
    endnote_p.add_argument("--out", type=Path, required=True)
    endnote_p.add_argument("--leftpanel", type=Path)
    endnote_p.add_argument("--url", dest="seed_url", default=None)
    endnote_p.add_argument("--max-pages", type=int)
    endnote_p.add_argument("--refresh-cache", action="store_true")
    endnote_p.add_argument("--emit-ris", action="store_true")
    endnote_p.add_argument("--emit-csl-json", action="store_true")
    endnote_p.add_argument("--emit-bibtex", action="store_true")
    endnote_p.add_argument(
        "--validate",
        action="store_true",
        help="Fail (non-zero) if referenced files are missing",
    )

    normalize_p = sub.add_parser(
        "normalize-export",
        help=(
            "Regenerate derived per-page variants (HTML/MD/TXT/JSON and "
            "endpoint response artifacts) for an existing export directory"
        ),
    )
    normalize_p.add_argument(
        "--in",
        dest="in_dir",
        type=Path,
        required=True,
        help="Existing export directory containing manifest.jsonl/pages/raw",
    )
    normalize_p.add_argument(
        "--validate",
        action="store_true",
        help="Fail (non-zero) if derived artifacts are still missing",
    )

    inspect_p = sub.add_parser(
        "inspect-export",
        help="Summarize and validate an existing export directory",
    )
    inspect_p.add_argument(
        "--in",
        dest="in_dir",
        type=Path,
        required=True,
        help="Existing export directory containing manifest.jsonl/pages/raw",
    )
    inspect_p.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON to stdout",
    )
    inspect_p.add_argument(
        "--fail-on-missing",
        action="store_true",
        help="Return non-zero if any referenced files are missing",
    )
    inspect_p.add_argument(
        "--max-missing-sample",
        type=int,
        default=25,
        help="Max missing paths to include in output",
    )

    args = parser.parse_args(argv)

    if args.cmd == "apis-report":
        try:
            if bool(args.crawl):
                report_data = collect_apis_report_data(export_dir=args.in_dir)
                endpoints = [
                    u
                    for u in report_data.endpoints
                    if u.startswith("https://data.uspto.gov/apis/")
                ]
                if endpoints:
                    max_pages = (
                        int(args.crawl_max_pages)
                        if args.crawl_max_pages is not None
                        else len(endpoints)
                    )
                    session = requests.Session()
                    http = HttpClient(
                        session,
                        timeout_s=int(args.crawl_timeout),
                    )
                    scope = UrlScope(
                        tuple(args.crawl_allow_host_suffix),
                        follow_offsite=bool(args.crawl_follow_offsite),
                    )
                    crawl_cfg = CrawlConfig(
                        out_dir=args.in_dir,
                        scope=scope,
                        max_pages=max_pages,
                        max_depth=0,
                        per_host_delay_s=float(args.crawl_per_host_delay),
                        respect_robots=not bool(args.crawl_no_robots),
                        refresh_cache=bool(args.crawl_refresh_cache),
                    )
                    crawler = Crawler(http=http, config=crawl_cfg)
                    crawler.crawl(endpoints, resume=False)

                ensure_export_html_variants(export_dir=args.in_dir)
                ensure_export_non_html_variants(export_dir=args.in_dir)
                ensure_export_api_endpoint_variants(export_dir=args.in_dir)

            report = write_apis_report(
                export_dir=args.in_dir,
                report_path=args.report_path,
            )
        except OSError as e:
            print(str(e), file=sys.stderr)
            return 2
        print(str(report))
        return 0

    if args.cmd == "crawl":
        session = requests.Session()
        http = HttpClient(session, timeout_s=args.timeout)

        scope = UrlScope(
            tuple(args.allow_host_suffix),
            follow_offsite=bool(args.follow_offsite),
        )
        crawl_cfg = CrawlConfig(
            out_dir=args.out,
            scope=scope,
            max_pages=int(args.max_pages),
            max_depth=int(args.max_depth),
            per_host_delay_s=float(args.per_host_delay),
            respect_robots=not bool(args.no_robots),
            refresh_cache=bool(args.refresh_cache),
        )
        crawler = Crawler(http=http, config=crawl_cfg)
        crawler.crawl(args.seed)

        ensure_export_html_variants(export_dir=args.out)
        ensure_export_non_html_variants(export_dir=args.out)
        ensure_export_api_endpoint_variants(export_dir=args.out)

        if args.emit_ris or args.emit_csl_json or args.emit_bibtex:
            citations_dir = args.out / "citations"
            if args.emit_ris:
                write_ris(crawler.citations, citations_dir / "refs.ris")
            if args.emit_csl_json:
                write_csl_json(
                    crawler.citations,
                    citations_dir / "refs.csl.json",
                )
            if args.emit_bibtex:
                write_bibtex(crawler.citations, citations_dir / "refs.bib")

        return 0

    if args.cmd == "normalize-export":
        try:
            html_n = ensure_export_html_variants(export_dir=args.in_dir)
            non_html_n = ensure_export_non_html_variants(export_dir=args.in_dir)
            endpoints_n = ensure_export_api_endpoint_variants(export_dir=args.in_dir)
        except OSError as e:
            print(str(e), file=sys.stderr)
            return 2

        print(
            "normalize-export: "
            f"html={html_n} non_html={non_html_n} endpoints={endpoints_n}"
        )
        if bool(args.validate):
            try:
                inspected = inspect_export(export_dir=args.in_dir)
            except (OSError, ValueError) as e:
                print(str(e), file=sys.stderr)
                return 2
            if inspected.missing_files:
                missing_msg = (
                    "normalize-export: missing_files=" f"{inspected.missing_files}"
                )
                print(
                    missing_msg,
                    file=sys.stderr,
                )
                return 4
        return 0

    if args.cmd == "inspect-export":
        try:
            inspected = inspect_export(
                export_dir=args.in_dir,
                max_missing_paths_sample=int(args.max_missing_sample),
            )
        except (OSError, ValueError) as e:
            print(str(e), file=sys.stderr)
            return 2

        if bool(args.json):
            import json as _json

            print(_json.dumps(inspected.to_dict(), indent=2))
        else:
            print(
                "inspect-export: "
                f"lines={inspected.lines_total} "
                f"invalid_json={inspected.lines_invalid_json} "
                f"referenced_files={inspected.referenced_files} "
                f"missing_files={inspected.missing_files}"
            )
            if inspected.missing_by_key:
                parts = " ".join(
                    f"{k}={v}" for k, v in inspected.missing_by_key.items()
                )
                print(f"inspect-export: missing_by_key: {parts}")
            if inspected.missing_paths_sample:
                print("inspect-export: missing_paths_sample:")
                for p in inspected.missing_paths_sample:
                    print(f"- {p}")

        if bool(args.fail_on_missing) and inspected.missing_files:
            return 4
        return 0

    if args.cmd == "uspto-data":
        # Manual seed workflow: use a browser-saved HTML snapshot to bootstrap
        # seeds without fetching WAF-protected entry pages.
        if args.seed_html is not None or args.seed_html_dir is not None:
            default_seed_url = args.seed_url or "https://data.uspto.gov/"

            def _infer_saved_from_url(html_text: str) -> str | None:
                m = re.search(
                    r"saved\s+from\s+url=\(\d+\)(https?://[^\s>]+)",
                    html_text,
                    flags=re.IGNORECASE,
                )
                if not m:
                    return None
                return m.group(1).strip()

            seed_paths: list[Path] = []
            if args.seed_html:
                seed_paths.extend([Path(p) for p in list(args.seed_html)])

            if args.seed_html_dir is not None:
                seed_html_dir = Path(args.seed_html_dir)
                if not seed_html_dir.exists():
                    print(
                        "--seed-html-dir does not exist",
                        file=sys.stderr,
                    )
                    return 2
                if not seed_html_dir.is_dir():
                    print(
                        "--seed-html-dir must be a directory",
                        file=sys.stderr,
                    )
                    return 2

                for seed_path in sorted(seed_html_dir.rglob("*.html")):
                    is_assets_dir = any(
                        part.lower().endswith("_files") for part in seed_path.parts
                    )
                    if is_assets_dir:
                        continue
                    seed_paths.append(seed_path)

            if not seed_paths:
                print(
                    "No seeds provided; use --seed-html or --seed-html-dir",
                    file=sys.stderr,
                )
                return 2

            session = requests.Session()
            http = HttpClient(session, timeout_s=args.timeout)
            scope = UrlScope(
                tuple(args.allow_host_suffix),
                follow_offsite=bool(args.follow_offsite),
            )
            crawl_cfg = CrawlConfig(
                out_dir=args.out,
                scope=scope,
                max_pages=int(args.max_pages),
                max_depth=int(args.max_depth),
                per_host_delay_s=float(args.per_host_delay),
                respect_robots=not bool(args.no_robots),
                refresh_cache=bool(args.refresh_cache),
            )
            crawler = Crawler(http=http, config=crawl_cfg)

            seeds: list[str] = []
            for seed_path in seed_paths:
                try:
                    raw = seed_path.read_bytes()
                except OSError as e:
                    print(
                        f"Failed to read seed HTML: {seed_path}: {e}",
                        file=sys.stderr,
                    )
                    continue

                html_text = raw.decode("utf-8", errors="replace")
                inferred_url = _infer_saved_from_url(html_text)
                page_url = inferred_url or default_seed_url

                # Avoid using pure WAF interstitials as seeds.
                if is_waf_challenge(
                    raw,
                    content_type="text/html",
                    allow_integration_heuristic=False,
                ):
                    msg = (
                        "Seed HTML looks like a WAF challenge; skipping: "
                        f"{seed_path}"
                    )
                    print(msg, file=sys.stderr)
                    continue

                # Ingest the local HTML into the output (so we keep an offline
                # copy even if subsequent HTTP fetches are blocked).
                crawler.ingest_local_html(url=page_url, body=raw)

                seeds.extend(
                    extract_links_from_html(
                        html_text,
                        page_url=page_url,
                    )
                )

            seeds = list(dict.fromkeys(seeds))
            if not seeds:
                print(
                    (
                        "No links found in provided seed HTML; "
                        "cannot bootstrap crawl."
                    ),
                    file=sys.stderr,
                )
                return 2

            crawler.crawl(seeds)

            ensure_export_html_variants(export_dir=args.out)
            ensure_export_non_html_variants(export_dir=args.out)
            ensure_export_api_endpoint_variants(export_dir=args.out)
            return 0

        uspto_cfg = USPTODataPortalConfig(
            out_dir=args.out,
            max_pages=int(args.max_pages),
            max_depth=int(args.max_depth),
            per_host_delay_s=float(args.per_host_delay),
            respect_robots=not bool(args.no_robots),
            refresh_cache=bool(args.refresh_cache),
        )
        summary = run_uspto(uspto_cfg)
        try:
            html_n = ensure_export_html_variants(export_dir=args.out)
            non_html_n = ensure_export_non_html_variants(export_dir=args.out)
            endpoints_n = ensure_export_api_endpoint_variants(export_dir=args.out)
        except OSError as e:
            print(str(e), file=sys.stderr)
            return 2

        stats = (summary or {}).get("stats") or {}
        fetched = int(stats.get("fetched") or 0)
        blocked_waf = int(stats.get("blocked_waf") or 0)
        blocked = int(stats.get("blocked") or 0)
        errors = int(stats.get("error") or 0)

        try:
            inspected = inspect_export(export_dir=args.out)
        except (OSError, ValueError) as e:
            print(str(e), file=sys.stderr)
            return 2

        msg = (
            "uspto-data: "
            f"fetched={fetched} blocked={blocked} blocked_waf={blocked_waf} "
            f"error={errors} normalize_html={html_n} "
            f"normalize_non_html={non_html_n} "
            f"normalize_endpoints={endpoints_n} "
            f"missing_files={inspected.missing_files}"
        )
        print(msg)
        if fetched == 0 and blocked_waf > 0:
            print(
                "Blocked by AWS WAF JS challenge; no pages fetched. "
                "See manifest.jsonl for details.",
                file=sys.stderr,
            )
            return 3
        if bool(args.validate) and inspected.missing_files:
            return 4
        return 0

    if args.cmd == "endnote25":
        seed_url = args.seed_url
        if seed_url is None:
            # Default matches existing script’s constant.
            from .exporters.endnote25_windows import DEFAULT_SEED_URL

            seed_url = DEFAULT_SEED_URL

        endnote_cfg = EndNoteExportConfig(
            out_dir=args.out,
            leftpanel_path=args.leftpanel,
            seed_url=seed_url,
            max_pages=args.max_pages,
            refresh_cache=bool(args.refresh_cache),
            emit_ris=bool(args.emit_ris),
            emit_csl_json=bool(args.emit_csl_json),
            emit_bibtex=bool(args.emit_bibtex),
        )
        session = requests.Session()
        exporter = EndNoteExporter(session=session, config=endnote_cfg)
        summary = exporter.export()
        pages = int((summary or {}).get("pages") or 0)
        try:
            inspected = inspect_export(export_dir=args.out)
        except (OSError, ValueError) as e:
            print(str(e), file=sys.stderr)
            return 2
        print(f"endnote25: pages={pages} " f"missing_files={inspected.missing_files}")
        if bool(args.validate) and inspected.missing_files:
            return 4
        return 0

    return 2
