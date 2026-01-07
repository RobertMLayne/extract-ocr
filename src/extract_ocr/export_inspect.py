from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ExportInspection:
    export_dir: Path
    lines_total: int
    lines_invalid_json: int
    kinds: dict[str, int]
    referenced_files: int
    missing_files: int
    missing_by_key: dict[str, int]
    missing_paths_sample: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "export_dir": str(self.export_dir),
            "lines_total": self.lines_total,
            "lines_invalid_json": self.lines_invalid_json,
            "kinds": dict(self.kinds),
            "referenced_files": self.referenced_files,
            "missing_files": self.missing_files,
            "missing_by_key": dict(self.missing_by_key),
            "missing_paths_sample": list(self.missing_paths_sample),
        }


_PATH_KEYS = {
    "raw",
    "page_md",
    "page_html",
    "page_txt",
    "page_json",
    "resp_md",
    "resp_html",
    "resp_txt",
    "resp_json",
}


def inspect_export(
    *,
    export_dir: Path,
    max_missing_paths_sample: int = 25,
) -> ExportInspection:
    export_dir = export_dir.resolve()
    manifest_jsonl = export_dir / "manifest.jsonl"
    manifest_json = export_dir / "manifest.json"
    if not manifest_jsonl.exists() and not manifest_json.exists():
        raise FileNotFoundError(
            f"Missing manifest.jsonl/manifest.json in: {export_dir}"
        )

    kinds: dict[str, int] = {}
    missing_by_key: dict[str, int] = {}

    referenced_files = 0
    missing_files = 0
    missing_paths_sample: list[str] = []

    lines_total = 0
    lines_invalid_json = 0

    if manifest_jsonl.exists():
        with manifest_jsonl.open("r", encoding="utf-8") as f:
            for line in f:
                lines_total += 1
                line = line.strip()
                if not line:
                    continue

                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    lines_invalid_json += 1
                    continue

                kind = str(evt.get("kind") or "")
                if kind:
                    kinds[kind] = kinds.get(kind, 0) + 1

                paths = evt.get("paths")
                if not isinstance(paths, dict):
                    continue

                for k, v in paths.items():
                    if k not in _PATH_KEYS:
                        continue
                    if not isinstance(v, str) or not v:
                        continue

                    referenced_files += 1

                    candidate = (export_dir / Path(v)).resolve()
                    # Keep validation local to export_dir.
                    try:
                        candidate.relative_to(export_dir)
                    except ValueError:
                        missing_files += 1
                        missing_by_key[k] = missing_by_key.get(k, 0) + 1
                        if len(missing_paths_sample) < max_missing_paths_sample:
                            missing_paths_sample.append(v)
                        continue

                    if not candidate.exists():
                        missing_files += 1
                        missing_by_key[k] = missing_by_key.get(k, 0) + 1
                        if len(missing_paths_sample) < max_missing_paths_sample:
                            missing_paths_sample.append(v)
    else:
        try:
            manifest_obj = json.loads(manifest_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON in manifest.json in: {export_dir}") from e

        items = manifest_obj.get("items")
        if isinstance(items, list):
            for it in items:
                if not isinstance(it, dict):
                    continue
                v = it.get("file")
                if not isinstance(v, str) or not v:
                    continue

                referenced_files += 1
                k = "file"

                candidate = (export_dir / Path(v)).resolve()
                try:
                    candidate.relative_to(export_dir)
                except ValueError:
                    missing_files += 1
                    missing_by_key[k] = missing_by_key.get(k, 0) + 1
                    if len(missing_paths_sample) < max_missing_paths_sample:
                        missing_paths_sample.append(v)
                    continue

                if not candidate.exists():
                    missing_files += 1
                    missing_by_key[k] = missing_by_key.get(k, 0) + 1
                    if len(missing_paths_sample) < max_missing_paths_sample:
                        missing_paths_sample.append(v)

    kinds = dict(sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0])))
    missing_by_key = dict(
        sorted(missing_by_key.items(), key=lambda kv: (-kv[1], kv[0]))
    )

    return ExportInspection(
        export_dir=export_dir,
        lines_total=lines_total,
        lines_invalid_json=lines_invalid_json,
        kinds=kinds,
        referenced_files=referenced_files,
        missing_files=missing_files,
        missing_by_key=missing_by_key,
        missing_paths_sample=missing_paths_sample,
    )
