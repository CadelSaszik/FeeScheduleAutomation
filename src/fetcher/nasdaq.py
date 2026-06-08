"""NASDAQ family fetcher: NOM, BX, PHLX, ISE, Gemini, Mercury.

Nasdaq fee pages are server-side rendered HTML on listingcenter.nasdaq.com.
Footnote markers appear as <span class="superscript">N</span> inside table
cells.  Footnote definitions appear below each table as text in the format
"[N] Definition text..."  We render superscripts explicitly so the AI can
match markers to definitions.

URL pattern: .../rulebook/<exchange>/rules/<exchange>-options-7
"""

from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup, Tag

from .base import BaseFetcher, FetchResult

logger = logging.getLogger(__name__)


class NasdaqFetcher(BaseFetcher):
    def extract_text(self, result: FetchResult) -> str:
        if not result.ok:
            return ""
        return _html_to_text(result.raw_bytes, result.exchange_id)


def _cell_text(cell: Tag) -> str:
    """Extract cell text, converting <span class="superscript"> to ^N^ marker."""
    parts = []
    for node in cell.children:
        if isinstance(node, Tag):
            if "superscript" in node.get("class", []):
                parts.append(f"^{node.get_text(strip=True)}^")
            else:
                parts.append(node.get_text(strip=True))
        else:
            text = str(node).strip()
            if text:
                parts.append(text)
    return " ".join(p for p in parts if p)


def _html_to_text(data: bytes, exchange_id: str) -> str:
    try:
        soup = BeautifulSoup(data, "lxml")

        # Remove chrome — keep only content area
        for tag in soup.find_all(["script", "style", "nav", "footer", "header"]):
            tag.decompose()

        # Render tables with superscript footnote markers preserved as ^N^
        table_blocks: list[str] = []
        for table in soup.find_all("table"):
            rows = []
            for tr in table.find_all("tr"):
                cells = [_cell_text(td) for td in tr.find_all(["th", "td"])]
                rows.append("\t".join(cells))
            table_blocks.append("[TABLE]\n" + "\n".join(rows) + "\n[/TABLE]")

        # Full body text — captures footnote definitions below tables
        # (format: "[N](#anchor) Definition text" or plain numbered list)
        body_text = soup.get_text(separator="\n", strip=True)

        # Label footnote definition paragraphs for Claude's Pass 1
        # Nasdaq footnote defs look like "[3] Some text..." or "3. Some text..."
        annotated_body = re.sub(
            r"(?m)^(\[?\d+\]?\.?\s+)(.{20,})",
            r"[FOOTNOTE DEF] \1\2",
            body_text,
        )

        parts = [annotated_body] + table_blocks
        return "\n\n".join(parts)
    except Exception as exc:
        logger.error("[%s] HTML extraction error: %s", exchange_id, exc)
        return ""
