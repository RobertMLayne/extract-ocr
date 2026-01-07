from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CitationItem:
    title: str
    url: str
    accessed: str  # YYYY-MM-DD or ISO
    local_path: str | None = None
    publisher: str | None = None
    author: str | None = None


def write_ris(items: list[CitationItem], out_path: Path) -> None:
    lines: list[str] = []
    for it in items:
        # Use ELEC/GEN to cover web help pages.
        lines.append("TY  - ELEC")
        lines.append(f"TI  - {it.title}")
        if it.author:
            lines.append(f"A1  - {it.author}")
        if it.publisher:
            lines.append(f"PB  - {it.publisher}")
        lines.append(f"UR  - {it.url}")
        lines.append(f"Y2  - {it.accessed}")
        if it.local_path:
            lines.append(f"L1  - {it.local_path}")
        lines.append("ER  - ")
        lines.append("")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        "\n".join(lines).rstrip() + "\n", encoding="utf-8", newline="\n"
    )


def write_csl_json(items: list[CitationItem], out_path: Path) -> None:
    csl: list[dict[str, object]] = []
    for it in items:
        entry: dict[str, object] = {
            "type": "webpage",
            "title": it.title,
            "URL": it.url,
            "accessed": {"raw": it.accessed},
        }
        if it.publisher:
            entry["publisher"] = it.publisher
        if it.author:
            entry["author"] = [{"literal": it.author}]
        if it.local_path:
            entry["note"] = f"Local copy: {it.local_path}"
        csl.append(entry)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(csl, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def write_bibtex(items: list[CitationItem], out_path: Path) -> None:
    # Minimal, robust BibTeX @online-like entries.
    lines: list[str] = []
    for idx, it in enumerate(items, start=1):
        key = f"ref{idx:04d}"
        lines.append(f"@online{{{key},")
        lines.append(f"  title = {{{it.title}}},")
        if it.author:
            lines.append(f"  author = {{{it.author}}},")
        if it.publisher:
            lines.append(f"  organization = {{{it.publisher}}},")
        lines.append(f"  url = {{{it.url}}},")
        lines.append(f"  urldate = {{{it.accessed}}},")
        if it.local_path:
            lines.append(f"  note = {{Local copy: {it.local_path}}},")
        lines.append("}")
        lines.append("")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        "\n".join(lines).rstrip() + "\n", encoding="utf-8", newline="\n"
    )
