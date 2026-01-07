---
name: Python
description: Python conventions for extract-ocr
applyTo: "**/*.py"
---
- Target Python 3.12.
- Prefer `pathlib.Path` for paths and keep paths Windows-safe.
- Default to `requests` + timeouts for HTTP; include retry/backoff only where needed.
- Avoid global state; make functions deterministic where practical.
- When writing files, use UTF-8 unless there is a strong reason otherwise.
