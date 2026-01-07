---
name: repo-doc-export-triage
description: Diagnose a failing export run (EndNote/pyUSPTO)
argument-hint: "paste terminal output"
agent: agent
---
You are diagnosing a failure in this repository's documentation export scripts.

Process:
1) Ask for/inspect the failing command and the terminal output.
2) Identify whether the failure is:
   - network/HTTP
   - HTML parsing
   - filesystem/path
   - encoding/decoding
   - dependency/import
3) Propose the smallest safe fix consistent with repo rules:
   - treat HTML/network as untrusted
   - avoid copying third-party doc content
   - keep changes scoped

If a code change is required, implement it and validate by re-running the smallest reproduction.
