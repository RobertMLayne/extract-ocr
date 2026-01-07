---
name: pyuspto-fetch-latest
description: Fetch and unpack latest pyUSPTO docs
agent: agent
---
Run the pyUSPTO fetch script using the repo virtualenv interpreter:

- `${workspaceFolder}\\.venv312\\Scripts\\python.exe docs\\pyuspto\\fetch_latest_docs.py`

Then report:
- whether the run succeeded
- the output folder (expected: `docs/pyuspto/local/latest/`)
