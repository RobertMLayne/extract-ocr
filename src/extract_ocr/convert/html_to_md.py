from __future__ import annotations

from bs4 import BeautifulSoup
from markdownify import markdownify as md


def _clean_soup_inplace(soup: BeautifulSoup) -> None:
    for tag_name in ["script", "style", "noscript"]:
        for t in soup.find_all(tag_name):
            t.decompose()


def _pick_main_content(soup: BeautifulSoup):
    for selector in [
        "main",
        "article",
        "#topic-content",
        "#topic",
        "#rh-topic",
        "div[role='main']",
        "div[role='document']",
    ]:
        node = soup.select_one(selector)
        if node and node.get_text(strip=True):
            return node

    best = None
    best_len = 0
    for div in soup.find_all("div"):
        text_len = len(div.get_text(" ", strip=True))
        if text_len > best_len:
            best = div
            best_len = text_len
    return best or soup.body or soup


def extract_title(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return h1.get_text(" ", strip=True)
    if soup.title and soup.title.get_text(strip=True):
        return soup.title.get_text(" ", strip=True)
    return "Untitled"


def html_to_markdown(html: str, *, source_url: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    _clean_soup_inplace(soup)
    main = _pick_main_content(soup)
    markdown = md(str(main), heading_style="ATX")
    markdown = markdown.strip() + "\n"
    return f"Source: {source_url}\n\n" + markdown
