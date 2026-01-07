# extract-ocr architecture

This document describes how `extract-ocr` crawls/ingests documentation-like content and produces stable offline export directories.

Notes:

- This repo can download third-party documentation; ensure you have rights/permission to store outputs.
- Avoid copying third-party documentation text into repo-authored docs; the architecture here is about the tooling and output structure.

## Big picture

This repo’s job is to take online (or locally saved) documentation-like content, crawl/ingest it, and produce a stable offline export directory containing:

- Raw fetched artifacts (`raw/`)
- Human-readable rendered outputs (`pages/`)
- A JSONL manifest event log tying everything together (`manifest.jsonl`)

The reusable engine lives in the Python package under `src/extract_ocr/`, driven via the CLI entrypoints.

## What an export directory is

Every crawl/export writes a folder containing:

- `manifest.jsonl`: append-only event log (one JSON object per fetched/blocked/ingested/rendered item)
- `raw/`: raw bytes captured from the network (or local ingest)
- `pages/`: derived/browsable artifacts (mostly `.md`, plus sidecars like `.txt`, `.json`, `.html`, and `*.resp.*`)

Precision: the effective “source of truth” is `manifest.jsonl` plus the referenced files in `raw/`. The `pages/` outputs are derived/regenerable from those inputs.

## Crawling and ingest flow

The crawler/ingestor is implemented under `src/extract_ocr/` and uses supporting modules for:

- URL scoping/normalization
- HTTP fetching and caching
- `robots.txt` handling
- Content sniffing (HTML vs JSON/XML/PDF/etc.)

During a crawl, each URL becomes a manifest event; raw bytes are stored; then derived “variants” are written into `pages/`.

## Variant generation (normalization passes)

Filesystem-level normalizers ("ensure*export*\*" passes) (re)generate derived files in `pages/` from `manifest.jsonl` + `raw/`:

- HTML variants: HTML → Markdown + sidecars
- Non-HTML variants: JSON/XML/PDF/text → readable Markdown + sidecars
- API endpoint variants: `https://data.uspto.gov/apis/...` endpoints → `*.resp.*` artifacts

These passes run after certain crawls, and can also be re-run against an existing export directory.

## Reports: linking endpoints to offline artifacts

The API endpoint report generator scans an export for discovered `https://data.uspto.gov/apis/...` endpoints and writes a Markdown report that links each endpoint to its local `pages/<stem>.resp.md`.

Behavior:

- Prefer manifest-provided response markdown paths when present and existing.
- Otherwise, fall back to a deterministic guessed path and mark it missing.

## CLI commands

The CLI provides user-facing commands that orchestrate crawling, normalization, inspection, and reporting. Key commands include:

- `crawl`: generic crawl/export
- `uspto-data`: opinionated crawl defaults for `data.uspto.gov` (including “seed from browser-saved HTML” workflows)
- `apis-report`: generate the endpoint report; optionally `--crawl` to fetch endpoints first
- `normalize-export`: regenerate derived `pages/` variants from an existing export directory
- `inspect-export`: summarize an export; optionally fail if referenced files are missing

## Validation semantics (export correctness)

- `inspect-export --fail-on-missing` exits non-zero if any referenced paths are missing.
- `normalize-export --validate` exits non-zero if referenced paths are still missing after regeneration.

## Scripts vs package

- `scripts/*.py` are convenience wrappers/workflows.
- The reusable engine is the library code under `src/extract_ocr/`.

EndNote precision: there are two EndNote paths:

- Standalone script: `scripts/export_endnote25_windows.py`
- Package exporter implementation: under `src/extract_ocr/` and driven via the package CLI
