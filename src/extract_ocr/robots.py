from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


class RobotsRules:
    """Very small robots.txt parser.

    Supports User-agent: * blocks with Allow/Disallow prefix matching.
    Conservative by design.
    """

    def __init__(self, raw_text: str) -> None:
        self._allow: list[str] = []
        self._disallow: list[str] = []

        active_for_star = False
        for line in raw_text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "#" in line:
                line = line.split("#", 1)[0].strip()
            if not line:
                continue

            key, _, value = line.partition(":")
            key = key.strip().lower()
            value = value.strip()

            if key == "user-agent":
                active_for_star = value == "*"
                continue

            if not active_for_star:
                continue

            if key == "disallow" and value:
                self._disallow.append(value)
            elif key == "allow" and value:
                self._allow.append(value)

        self._allow.sort(key=len, reverse=True)
        self._disallow.sort(key=len, reverse=True)

    def can_fetch(self, url: str) -> bool:
        path = urlparse(url).path or "/"
        for allow_prefix in self._allow:
            if path.startswith(allow_prefix):
                return True
        for disallow_prefix in self._disallow:
            if path.startswith(disallow_prefix):
                return False
        return True


@dataclass
class RobotsCache:
    robots_dir: Path

    def __post_init__(self) -> None:
        self.robots_dir.mkdir(parents=True, exist_ok=True)

    def _path_for_host(self, host: str) -> Path:
        host = host.lower().replace(":", "_")
        return self.robots_dir / f"{host}.txt"

    def load(self, host: str) -> RobotsRules | None:
        path = self._path_for_host(host)
        if not path.exists():
            return None
        return RobotsRules(path.read_text(encoding="utf-8", errors="replace"))

    def store(self, host: str, text: str) -> None:
        self._path_for_host(host).write_text(
            text,
            encoding="utf-8",
            newline="\n",
        )
