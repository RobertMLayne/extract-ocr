---
name: Docs Export
description: Guidance for web-doc ingestion/export code and artifacts
applyTo: "scripts/**/*.py,docs/**/*.py"
---
- Avoid embedding or reprinting third-party doc content in the repo outside of generated export outputs.
- Favor resumable/idempotent exports: stable filenames, manifests, caching.
- Be strict about encoding/decoding: decode bytes intentionally and validate output.
- Prefer rewriting links to keep offline artifacts navigable.
- Keep generated artifacts excluded from search/watchers where possible.
