"""
Fetch the latest pyUSPTO docs from Read the Docs and produce local offline
assets.

Outputs go into docs/pyUSPTO/local/latest/.
"""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path
from typing import Iterable
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup
from html2text import HTML2Text

BASE_URLS = {
    "pdf": "https://pyuspto.readthedocs.io/_/downloads/en/latest/pdf/",
    "htmlzip": "https://pyuspto.readthedocs.io/_/downloads/en/latest/htmlzip/",
    "singlehtml": ("https://pyuspto.readthedocs.io/_/downloads/en/latest/singlehtml/"),
}

DEST = Path(__file__).parent / "local" / "latest"
PDF_PATH = DEST / "pyuspto-latest.pdf"
HTMLZIP_PATH = DEST / "pyuspto-latest-html.zip"
SINGLEHTML_ZIP_PATH = DEST / "pyuspto-latest-singlehtml.zip"
SINGLEHTML_EXTRACT = DEST / "singlehtml"
HTML_EXTRACT = DEST / "html"
SINGLE_HTML_OUT = DEST / "pyuspto-latest.html"
MARKDOWN_OUT = DEST / "pyuspto-latest.md"
TEXT_OUT = DEST / "pyuspto-latest.txt"


def reset_dir(path: Path) -> None:
    """Replace an output directory to avoid stale content."""
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def download(url: str, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    request = Request(url, headers={"User-Agent": "python"})
    with urlopen(request, timeout=90) as response, target.open("wb") as fh:
        shutil.copyfileobj(response, fh)
    return target


def download_artifacts() -> None:
    download(BASE_URLS["pdf"], PDF_PATH)
    download(BASE_URLS["htmlzip"], HTMLZIP_PATH)
    download(BASE_URLS["singlehtml"], SINGLEHTML_ZIP_PATH)


def extract_zip(zip_path: Path, destination: Path) -> None:
    reset_dir(destination)
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(destination)


def pick_single_html(zip_path: Path, extract_dir: Path, output_path: Path) -> Path:
    extract_zip(zip_path, extract_dir)
    with zipfile.ZipFile(zip_path) as archive:
        html_candidates: list[str] = [
            name for name in archive.namelist() if name.lower().endswith(".html")
        ]
    if not html_candidates:
        raise RuntimeError("No HTML files found in singlehtml archive")
    preferred = sorted(
        html_candidates,
        key=lambda name: (0 if "index" in name.lower() else 1, len(name)),
    )[0]
    source = extract_dir / preferred
    if not source.exists():
        # Handle zipped paths that include a leading folder by rebuilding the
        # path manually.
        source = extract_dir / Path(preferred)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(source.read_bytes())
    return output_path


def html_to_markdown(html_path: Path, output_path: Path) -> None:
    html = html_path.read_text(encoding="utf-8")
    converter = HTML2Text()
    converter.ignore_links = False
    converter.body_width = 0
    markdown = converter.handle(html)
    output_path.write_text(markdown, encoding="utf-8")


def html_to_text(html_path: Path, output_path: Path) -> None:
    html = html_path.read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")
    raw_lines: Iterable[str] = soup.get_text("\n").splitlines()
    lines = [line.strip() for line in raw_lines if line.strip()]
    text = "\n".join(lines)
    output_path.write_text(text, encoding="utf-8")


def main() -> None:
    DEST.mkdir(parents=True, exist_ok=True)
    download_artifacts()
    extract_zip(HTMLZIP_PATH, HTML_EXTRACT)
    single_html = pick_single_html(
        SINGLEHTML_ZIP_PATH, SINGLEHTML_EXTRACT, SINGLE_HTML_OUT
    )
    html_to_markdown(single_html, MARKDOWN_OUT)
    html_to_text(single_html, TEXT_OUT)
    outputs = {
        "pdf": PDF_PATH,
        "html zip": HTMLZIP_PATH,
        "singlehtml zip": SINGLEHTML_ZIP_PATH,
        "html": single_html,
        "markdown": MARKDOWN_OUT,
        "text": TEXT_OUT,
    }
    for label, path in outputs.items():
        print(f"{label}: {path.relative_to(Path(__file__).parent)}")


if __name__ == "__main__":
    main()
