---
name: endnote-export-smoke
description: Run EndNote exporter (smoke: 5 pages)
argument-hint: "out=<path>"
agent: agent
---
Run the EndNote exporter smoke test.

Requirements:
- Use the repo virtualenv interpreter: `${workspaceFolder}\\.venv312\\Scripts\\python.exe`
- Default output folder: `docs/endnote/export/endnote25-windows-smoke`
- Pass `--clean`.

If the user supplied `out=...` in the chat input, use that as the output folder.

Then report:
- whether the run succeeded
- how many pages were exported
- the output folder path
