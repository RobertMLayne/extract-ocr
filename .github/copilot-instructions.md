# extract-ocr – Copilot instructions

## What this repo is

- Python utilities that download and convert documentation into local/offline artifacts.
- Main workflows:
  - EndNote web help exporter: `scripts/export_endnote25_windows.py`
  - pyUSPTO doc fetcher: `docs/pyUSPTO/fetch_latest_docs.py`

## Non-negotiables

- Do not paste or reproduce large chunks of third‑party documentation content in code reviews, comments, or generated documentation.
- When exporting third‑party docs, preserve attribution/links and ensure the user has rights/permission to store the output.
- Prefer minimal, focused changes; avoid unrelated refactors.

## How to run (Windows)

- Use the repo virtualenv interpreter: `${workspaceFolder}\.venv312\Scripts\python.exe`
- EndNote exporter smoke test (also available as a VS Code task):
  - `python scripts/export_endnote25_windows.py --leftpanel "docs/EndNote 25/endnote25_windows_leftpanel.html" --out "docs/EndNote 25/export/endnote25-windows-smoke" --max-pages 5 --clean --validate`
- Full EndNote export:
  - `python scripts/export_endnote25_windows.py --leftpanel "docs/EndNote 25/endnote25_windows_leftpanel.html" --out "docs/EndNote 25/export/endnote25-windows" --clean`
- pyUSPTO fetch:
  - `python docs/pyUSPTO/fetch_latest_docs.py`

### VS Code tasks

- `EndNote: export (smoke 5 pages, strict)`
  - Runs `ruff` + `pyright` + export `--validate`
- `EndNote: export (smoke 5 pages, quick)`
  - No dev tools required; still runs export `--validate`

### Optional dev-only checks

To enable the dev-only VS Code tasks that run CLI lint/type checks (ruff/pyright):

- `python -m pip install -r requirements-dev.txt`

## Code style

- Python: readable, explicit names, type hints when it improves clarity.
- Prefer small pure functions, and keep I/O at the edges.
- Avoid adding new dependencies unless they clearly improve correctness/robustness.

## When editing exporters

- Treat networking/HTML as untrusted input: retry/backoff, timeouts, and defensive parsing.
- Keep output paths stable and portable (forward slashes in Markdown links).
- If changing output format, update `README.md` and any VS Code tasks that run the scripts.
