"""MEMX Options fetcher.

MEMX publishes fee schedules on an HTML page; some versions are also
available as PDFs linked from that page. We attempt to fetch both the
HTML landing page and any linked PDF.
"""

from __future__ import annotations

import io
import logging
import re

import pdfplumber
import requests
from bs4 import BeautifulSoup

from .base import BaseFetcher, FetchResult, HEADERS, TIMEOUT

logger = logging.getLogger(__name__)


class MemxFetcher(BaseFetcher):
    def fetch(self) -> FetchResult:
        result = super().fetch()
        if not result.ok:
            return result

        # Try to find a PDF link on the landing page and prefer that
        try:
            soup = BeautifulSoup(result.raw_bytes, "lxml")
            pdf_link = None
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if re.search(r"\.pdf", href, re.IGNORECASE):
                    pdf_link = href if href.startswith("http") else "https://info.memxtrading.com" + href
                    break
            if pdf_link:
                logger.info("[memx] Found PDF link: %s", pdf_link)
                resp = requests.get(pdf_link, headers=HEADERS, timeout=TIMEOUT)
                resp.raise_for_status()
                result.raw_bytes = resp.content
                result.content_type = "pdf"
                result.file_path.with_suffix(".pdf").write_bytes(resp.content)
        except Exception as exc:
            logger.warning("[memx] Could not fetch PDF, falling back to HTML: %s", exc)

        return result

    def extract_text(self, result: FetchResult) -> str:
        if not result.ok:
            return ""
        if result.content_type == "pdf":
            return _pdf_to_text(result.raw_bytes)
        return _html_to_text(result.raw_bytes)


def _pdf_to_text(data: bytes) -> str:
    pages = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for i, page in enumerate(pdf.pages):
            text = page.extract_text(x_tolerance=3, y_tolerance=3) or ""
            for table in page.extract_tables():
                rows = ["\t".join(cell or "" for cell in row) for row in table]
                text += "\n[TABLE]\n" + "\n".join(rows) + "\n[/TABLE]\n"
            pages.append(f"--- Page {i + 1} ---\n{text}")
    return "\n".join(pages)


def _html_to_text(data: bytes) -> str:
    soup = BeautifulSoup(data, "lxml")
    for tag in soup.find_all(["script", "style", "nav", "footer"]):
        tag.decompose()
    parts = [soup.get_text(separator="\n", strip=True)]
    for table in soup.find_all("table"):
        rows = ["\t".join(td.get_text(strip=True) for td in tr.find_all(["th", "td"]))
                for tr in table.find_all("tr")]
        parts.append("[TABLE]\n" + "\n".join(rows) + "\n[/TABLE]")
    return "\n\n".join(parts)
