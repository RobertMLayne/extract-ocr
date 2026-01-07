---
name: endnote-export-full
description: Run EndNote exporter (full)
argument-hint: "out=<path>"
agent: agent
---
Run the EndNote exporter full run.

Requirements:
- Use the repo virtualenv interpreter: `${workspaceFolder}\\.venv312\\Scripts\\python.exe`
- Default output folder: `docs/endnote/export/endnote25-windows`
- Pass `--clean`.

If the user supplied `out=...` in the chat input, use that as the output folder.

Then report:
- whether the run succeeded
- how many pages were exported
- the output folder path
