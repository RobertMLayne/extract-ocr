from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class CrawlState:
    state_dir: Path

    def __post_init__(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.queue_path = self.state_dir / "queue_urls.txt"
        self.done_path = self.state_dir / "done_urls.txt"
        self.failed_path = self.state_dir / "failed_urls.txt"

    def load_set(self, path: Path) -> set[str]:
        if not path.exists():
            return set()
        return {
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }

    def load_queue(self) -> list[str]:
        if not self.queue_path.exists():
            return []
        return [
            line.strip()
            for line in self.queue_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def save_queue(self, urls: list[str]) -> None:
        self.queue_path.write_text(
            "\n".join(urls) + ("\n" if urls else ""),
            encoding="utf-8",
            newline="\n",
        )

    def append_line(self, path: Path, url: str) -> None:
        with path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(url + "\n")
