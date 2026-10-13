"""PDF text extraction that is aware of hidden (white / micro-font / off-page) text layers.

The visible text is what a human sees (and what citations are verified against).
The hidden text is the classic carrier of *indirect prompt injection* in PDFs.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pdfplumber


@dataclass
class PageContent:
    page: int  # 1-based
    text: str  # visible text only
    hidden_text: str  # text a human cannot see (white-on-white, tiny, off-page)

    @property
    def full_text(self) -> str:
        return (self.text + ("\n" + self.hidden_text if self.hidden_text else "")).strip()


def _is_white(color: Any) -> bool:
    if color is None:
        return False
    if isinstance(color, (int, float)):
        return float(color) >= 0.97
    try:
        comps = [float(c) for c in color]
    except (TypeError, ValueError):
        return False
    if not comps:
        return False
    if len(comps) == 4:  # CMYK: white == all zero
        return all(c <= 0.03 for c in comps)
    return all(c >= 0.97 for c in comps)


def _char_hidden(ch: dict, page_w: float, page_h: float) -> bool:
    # NB: whitespace glyphs are judged by their own colour/size too, so hidden text keeps its spaces.
    if _is_white(ch.get("non_stroking_color")):
        return True
    if float(ch.get("size", 10)) < 2.0:
        return True
    if ch["x0"] < -1 or ch["x1"] > page_w + 1 or ch["top"] < -1 or ch["bottom"] > page_h + 1:
        return True
    return False


def extract_pdf(path: str | Path) -> list[PageContent]:
    pages: list[PageContent] = []
    with pdfplumber.open(str(path)) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            w, h = float(page.width), float(page.height)
            visible = page.filter(lambda o: o.get("object_type") != "char" or not _char_hidden(o, w, h))
            hidden = page.filter(lambda o: o.get("object_type") == "char" and _char_hidden(o, w, h))
            vtxt = (visible.extract_text() or "").strip()
            htxt = (hidden.extract_text() or "").strip() if hidden.chars else ""
            pages.append(PageContent(page=i, text=vtxt, hidden_text=htxt))
    return pages


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
