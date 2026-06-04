"""MIAX family fetcher: MIAX, Pearl, Emerald, Sapphire.

All publish PDFs via miaxoptions.com.
"""

from __future__ import annotations

import io
import logging

import pdfplumber

from .base import BaseFetcher, FetchResult

logger = logging.getLogger(__name__)


class MiaxFetcher(BaseFetcher):
    def extract_text(self, result: FetchResult) -> str:
        if not result.ok:
            return ""
        return _pdf_to_text(result.raw_bytes, result.exchange_id)


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
