# extract-ocr

Small utilities for producing local/offline documentation artifacts.

- Architecture overview: [ARCHITECTURE.md](ARCHITECTURE.md)

## Git LFS (required for this repo)

This repo stores large/binary documentation artifacts in Git LFS (not regular Git blobs).

Before cloning/pulling, install Git LFS and run:

```powershell
git lfs install
```

If Git LFS is not installed, you will see pointer files instead of the real binaries.

## Copilot + VS Code customization

This repo includes Copilot instruction and prompt files for repeatable workflows.

Instructions:

- Repo-wide: `.github/copilot-instructions.md`
- Scoped: `.github/instructions/*.instructions.md`
- Agent notes: `AGENTS.md`

Prompts (type `/` in Copilot Chat):

- `/endnote-export-smoke`
- `/endnote-export-full`
- `/pyuspto-fetch-latest`
- `/repo-doc-export-triage`

MCP servers:

- Workspace MCP configuration stub is in `.vscode/mcp.json`.
- Enable the MCP server gallery via the workspace setting `chat.mcp.gallery.enabled` (already set in `.vscode/settings.json`).

### Global profile sync (Default + Robert)

This environment can't directly edit your `%APPDATA%` VS Code profile files, so the repo includes a local apply script:

- Template: `vscode/global/settings.copilot.json`
- Apply script: `vscode/global/Apply-VSCodeProfileSettings.ps1`

Run (PowerShell):

```powershell
./vscode/global/Apply-VSCodeProfileSettings.ps1
```

By default, it updates profiles named `Default` and `Robert` (when profile metadata is available), and it creates timestamped backups of any settings files it changes.

### Pylance notification: "… file(s) and … cells to analyze"

If you see a persistent Pylance notification like "1 file and 0 cells to analyze", this is typically a Pylance UI/status bug.

This workspace enables a mitigation:

- `python.analysis.enablePytestSupport: true`

If you still see it:

- Run `Python: Restart Language Server` (Command Palette)
- Or run `Developer: Reload Window`
- Optional: run `Pylance: Clear All Persisted Indices` then reload
- Update the Pylance extension to the latest version
- Reset Python/Pylance workspace caches (last resort): `vscode/global/Reset-PythonPylanceWorkspaceState.ps1`

VS Code tasks are also available:

- `Pylance: reset workspace state (dry run)`
- `Pylance: reset workspace state (apply)`
- `Pylance: reset workspace state (apply + clear global storage)`

Reset caches (PowerShell):

```powershell
# Dry run first
./vscode/global/Reset-PythonPylanceWorkspaceState.ps1 -WorkspacePath "C:\\Dev\\Projects\\extract-ocr"

# Apply deletion
./vscode/global/Reset-PythonPylanceWorkspaceState.ps1 -WorkspacePath "C:\\Dev\\Projects\\extract-ocr" -Apply
```

## EndNote 2025 (EndNote 25) Windows exporter

Input TOC HTML: `docs/EndNote 25/endnote25_windows_leftpanel.html`

Run:

```powershell
C:/Dev/Projects/extract-ocr/.venv312/Scripts/python.exe scripts/export_endnote25_windows.py `
  --leftpanel "docs/EndNote 25/endnote25_windows_leftpanel.html" `
  --out "docs/EndNote 25/export/endnote25-windows" `
  --clean
```

Outputs:

- `docs/EndNote 25/export/endnote25-windows/endnote25-windows.md` (consolidated)
- `docs/EndNote 25/export/endnote25-windows/pages/*.md` (per-page)
- `docs/EndNote 25/export/endnote25-windows/manifest.json` (URL ↔ file mapping)

Notes:

- This downloads third-party documentation; ensure you have rights/permission to store it.
- By default it caches fetched HTML in `docs/EndNote 25/export/endnote25-windows/.cache/`.

## pyUSPTO latest docs fetch

Run:

```powershell
C:/Dev/Projects/extract-ocr/.venv312/Scripts/python.exe docs/pyUSPTO/fetch_latest_docs.py
```

Outputs go to `docs/pyUSPTO/local/latest/`.

## USPTO data.uspto.gov ingest

There is a general-purpose ingestor for `https://data.uspto.gov/` that crawls from seed URLs, respects `robots.txt` by default, rate-limits per host, and writes a resumable export with a JSONL manifest.

See also: [ARCHITECTURE.md](ARCHITECTURE.md)

Run (PowerShell):

```powershell
C:/Dev/Projects/extract-ocr/.venv312/Scripts/python.exe scripts/ingest_data_uspto_gov.py \
  --out docs/USPTO\ ODP/export/data-uspto-gov-smoke \
  --max-pages 50
```

Outputs:

- `docs/USPTO ODP/export/<name>/manifest.jsonl` (one line per fetched/blocked/error item)
- `docs/USPTO ODP/export/<name>/manifest.json` (summary + stats)
- `docs/USPTO ODP/export/<name>/raw/` (raw fetched artifacts)
- `docs/USPTO ODP/export/<name>/pages/` (HTML pages converted to Markdown)

If you have an ODP API key, set `USPTO_ODP_API_KEY` to allow authenticated requests to `api.uspto.gov` where relevant.

### Normalize an existing export

If you already have an export directory and want to (re)generate derived artifacts (per-page markdown, endpoint response variants, etc.) without crawling:

```powershell
C:/Dev/Projects/extract-ocr/.venv312/Scripts/python.exe -m extract_ocr normalize-export --in "docs/USPTO ODP/export/data-uspto-gov-smoke"
```

### Inspect/validate an export

Summarize an export and optionally fail if any referenced files are missing:

```powershell
C:/Dev/Projects/extract-ocr/.venv312/Scripts/python.exe -m extract_ocr inspect-export --in "docs/USPTO ODP/export/data-uspto-gov-smoke"

# Strict mode (non-zero exit if missing)
C:/Dev/Projects/extract-ocr/.venv312/Scripts/python.exe -m extract_ocr inspect-export --in "docs/USPTO ODP/export/data-uspto-gov-smoke" --fail-on-missing
```

Validate after normalization:

```powershell
C:/Dev/Projects/extract-ocr/.venv312/Scripts/python.exe -m extract_ocr normalize-export --in "docs/USPTO ODP/export/data-uspto-gov-smoke" --validate
```
