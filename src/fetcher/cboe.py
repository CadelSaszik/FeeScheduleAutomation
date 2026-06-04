"""CBOE family fetcher: EDGX, BZX, C2, CBOE C1.

CBOE exposes a structured CSV export endpoint on each fee schedule page:
  https://www.cboe.com/us/options/membership/fee_schedule/<exchange>/?csv=true&feedate=YYYY-MM-DD

This returns a clean 3-column CSV (Code, Description, Fee) covering all 72+
fee codes for that exchange on that date. This is far more reliable than
parsing a PDF whose URL changes with every update.
"""

from __future__ import annotations

import io
import logging
from datetime import date
from pathlib import Path

from .base import BaseFetcher, FetchResult

logger = logging.getLogger(__name__)


class CboeFetcher(BaseFetcher):
    def fetch(self) -> FetchResult:
        today = date.today().strftime("%Y-%m-%d")
        csv_url = self.fee_url.rstrip("/") + f"?csv=true&feedate={today}"

        # Temporarily point the base fetcher at the CSV URL
        original_url = self.fee_url
        self.fee_url = csv_url
        result = super().fetch()
        self.fee_url = original_url

        # Re-stamp the content type so downstream code knows it's CSV
        if result.ok:
            result.content_type = "csv"
            # Rename the saved file to .csv if it was saved as .pdf
            if result.file_path.suffix == ".pdf":
                csv_path = result.file_path.with_suffix(".csv")
                result.file_path.rename(csv_path)
                result.file_path = csv_path

        return result

    def extract_text(self, result: FetchResult) -> str:
        if not result.ok:
            return ""
        try:
            return result.raw_bytes.decode("utf-8", errors="replace")
        except Exception as exc:
            logger.error("[%s] CSV decode error: %s", self.exchange_id, exc)
            return ""
