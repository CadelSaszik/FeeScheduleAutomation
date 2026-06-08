"""BOX Options Exchange fetcher.

BOX moved to boxexchange.com and now publishes date-stamped PDFs
(e.g. BOX-Fee-Schedule-as-of-April-24-2026-1.pdf). The fee_url in config
points to the stable fee schedule landing page; this fetcher discovers the
current PDF URL from that page at runtime.
"""

from __future__ import annotations

import io
import logging
from urllib.parse import urljoin

import pdfplumber
from bs4 import BeautifulSoup

from .base import BaseFetcher, FetchResult, TIMEOUT

logger = logging.getLogger(__name__)


class BoxFetcher(BaseFetcher):
    def fetch(self) -> FetchResult:
        """Discover current PDF URL from landing page, then download it."""
        pdf_url = self._discover_pdf_url()
        if pdf_url:
            logger.info("[%s] Discovered PDF URL: %s", self.exchange_id, pdf_url)
            original_url = self.fee_url
            self.fee_url = pdf_url
            result = super().fetch()
            self.fee_url = original_url
        else:
            logger.warning(
                "[%s] Could not discover PDF URL from %s — fetching landing page directly",
                self.exchange_id, self.fee_url,
            )
            result = super().fetch()
        return result

    def extract_text(self, result: FetchResult) -> str:
        if not result.ok:
            return ""
        return _pdf_to_text(result.raw_bytes, self.exchange_id)

    def _discover_pdf_url(self) -> str | None:
        try:
            resp = self.session.get(self.fee_url, timeout=TIMEOUT)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.content, "lxml")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "fee" in href.lower() and href.lower().endswith(".pdf"):
                    return href if href.startswith("http") else urljoin(self.fee_url, href)
        except Exception as exc:
            logger.warning("[%s] PDF discovery failed: %s", self.exchange_id, exc)
        return None


def _pdf_to_text(data: bytes, exchange_id: str) -> str:
    pages = []
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for i, page in enumerate(pdf.pages):
                text = page.extract_text(x_tolerance=3, y_tolerance=3) or ""
                for table in page.extract_tables():
                    rows = ["\t".join(cell or "" for cell in row) for row in table]
                    text += "\n[TABLE]\n" + "\n".join(rows) + "\n[/TABLE]\n"
                pages.append(f"--- Page {i + 1} ---\n{text}")
    except Exception as exc:
        logger.error("[%s] PDF extraction error: %s", exchange_id, exc)
        return ""
    return "\n".join(pages)
