"""CBOE family fetcher: EDGX, BZX, C2, CBOE C1.

CBOE publishes fee schedules at stable HTML landing pages:
  https://www.cboe.com/us/options/membership/fee_schedule/<exchange>/

The actual PDF URL is embedded in that page and changes with every update
(the filename includes the effective date). This fetcher:
  1. Loads the HTML landing page to find the current PDF link
  2. Downloads the PDF from cdn.cboe.com
  3. Extracts text + tables for AI ingestion
"""

from __future__ import annotations

import io
import logging
import re

import pdfplumber
from bs4 import BeautifulSoup

from .base import BaseFetcher, FetchResult, HEADERS, TIMEOUT

logger = logging.getLogger(__name__)

_CDN_BASE = "https://cdn.cboe.com"
_CBOE_BASE = "https://www.cboe.com"


class CboeFetcher(BaseFetcher):
    def fetch(self) -> FetchResult:
        # Step 1: load the HTML landing page to find the current PDF URL
        pdf_url = self._discover_pdf_url(self.fee_url)
        if pdf_url:
            logger.info("[%s] Discovered PDF URL: %s", self.exchange_id, pdf_url)
            # Temporarily override the fee_url so the base fetch() downloads the PDF
            original_url = self.fee_url
            self.fee_url = pdf_url
            result = super().fetch()
            self.fee_url = original_url
            return result
        else:
            logger.warning(
                "[%s] Could not find PDF link on %s — falling back to direct fetch",
                self.exchange_id, self.fee_url,
            )
            return super().fetch()

    def extract_text(self, result: FetchResult) -> str:
        if not result.ok:
            return ""
        return _pdf_to_text(result.raw_bytes, self.exchange_id)

    # ------------------------------------------------------------------

    def _discover_pdf_url(self, landing_url: str) -> str | None:
        """Load the CBOE fee schedule landing page and return the PDF href."""
        try:
            resp = self.session.get(landing_url, timeout=TIMEOUT, allow_redirects=True)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("[%s] Could not load landing page %s: %s",
                           self.exchange_id, landing_url, exc)
            return None

        soup = BeautifulSoup(resp.content, "lxml")

        # Look for any <a> whose href points to a fee schedule PDF on the CDN.
        # CBOE filenames typically contain "Fee-Schedule" and end in .pdf.
        for a in soup.find_all("a", href=True):
            href: str = a["href"]
            if re.search(r"fee.schedule.*\.pdf", href, re.IGNORECASE):
                if href.startswith("http"):
                    pdf_url = href
                elif href.startswith("/"):
                    pdf_url = _CDN_BASE + href if "cdn" in href else _CBOE_BASE + href
                else:
                    continue
                # Set Referer to the landing page so the CDN accepts the PDF request
                self.session.headers["Referer"] = landing_url
                return pdf_url
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
