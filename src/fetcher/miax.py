"""MIAX family fetcher: MIAX, Pearl, Emerald, Sapphire.

MIAX moved from miaxoptions.com to miaxglobal.com and now publishes
date-stamped PDFs (e.g. MIAX_Options_Fee_Schedule_06012026.pdf).
Each exchange has its own stable landing page; this fetcher discovers
the current PDF link from that page at runtime so URL updates are not
needed when MIAX publishes a new fee schedule.
"""

from __future__ import annotations

import io
import logging
from urllib.parse import urljoin

import pdfplumber
from bs4 import BeautifulSoup

from .base import BaseFetcher, FetchResult, TIMEOUT

logger = logging.getLogger(__name__)


class MiaxFetcher(BaseFetcher):
    def fetch(self) -> FetchResult:
        """Discover current fee-schedule PDF from the landing page, then download it."""
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
        return _pdf_to_text(result.raw_bytes, result.exchange_id)

    def _discover_pdf_url(self) -> str | None:
        """Find the first non-highlight PDF link on the per-exchange landing page."""
        try:
            resp = self.session.get(self.fee_url, timeout=TIMEOUT)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.content, "lxml")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if href.lower().endswith(".pdf") and "highlight" not in href.lower():
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
