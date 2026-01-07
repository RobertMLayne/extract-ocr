from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def relpath_posix(path: Path, base_dir: Path) -> str:
    rel = path.relative_to(base_dir)
    return rel.as_posix()


@dataclass
class ManifestWriter:
    out_dir: Path

    def __post_init__(self) -> None:
        self.jsonl_path = self.out_dir / "manifest.jsonl"
        self.json_path = self.out_dir / "manifest.json"

    def append(self, event: dict[str, Any]) -> None:
        event = dict(event)
        event.setdefault("at", utc_iso())
        self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        with self.jsonl_path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def write_summary(self, summary: dict[str, Any]) -> None:
        self.json_path.write_text(
            json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
        )
