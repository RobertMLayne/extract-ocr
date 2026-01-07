from __future__ import annotations

import os


def _present(name: str) -> bool:
    value = os.getenv(name)
    return bool(value and value.strip())


def main() -> None:
    checks: list[tuple[str, str]] = [
        ("GITHUB_TOKEN", "Used by MCP server: github"),
        ("FIRECRAWL_API_KEY", "Used by MCP server: firecrawl"),
        ("CONTEXT7_API_KEY", "Used by MCP server: context7"),
    ]

    ok = True
    for env_name, purpose in checks:
        is_present = _present(env_name)
        status = "OK" if is_present else "MISSING"
        print(f"{status}: {env_name} — {purpose}")
        ok = ok and is_present

    if not ok:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
