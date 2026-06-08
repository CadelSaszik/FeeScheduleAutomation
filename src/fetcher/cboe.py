"""CBOE family fetcher: EDGX, BZX, C2, CBOE C1.

CBOE exposes a structured CSV export endpoint on each fee schedule page:
  https://www.cboe.com/us/options/membership/fee_schedule/<exchange>/?csv=true&feedate=YYYY-MM-DD

This returns a clean 3-column CSV (Code, Description, Fee) covering all 72+
fee codes for that exchange on that date.

Footnotes are not present in the CSV. After fetching the CSV we also fetch the
HTML landing page, which may contain footnote content embedded in the page HTML
or in a Next.js __NEXT_DATA__ JSON blob. That supplemental text is passed to the
AI extractor so it can link footnotes to the correct fee codes.
"""

from __future__ import annotations

import logging
import re
from datetime import date

from bs4 import BeautifulSoup

from .base import BaseFetcher, FetchResult, TIMEOUT

logger = logging.getLogger(__name__)

_MIN_SUPPLEMENTAL_CHARS = 200  # below this we treat the HTML as empty/useless


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
            if result.file_path.suffix == ".pdf":
                csv_path = result.file_path.with_suffix(".csv")
                result.file_path.rename(csv_path)
                result.file_path = csv_path

            # Fetch the HTML landing page for footnote content
            result.supplemental_text = self._fetch_html_supplemental(original_url)

        return result

    def extract_text(self, result: FetchResult) -> str:
        if not result.ok:
            return ""
        try:
            return result.raw_bytes.decode("utf-8", errors="replace")
        except Exception as exc:
            logger.error("[%s] CSV decode error: %s", self.exchange_id, exc)
            return ""

    def _fetch_html_supplemental(self, html_url: str) -> str:
        """Fetch the HTML fee schedule page and extract a structured footnote manifest.

        CBOE fee schedule HTML (confirmed static, not a SPA) contains:
        - A fee table: id="fee-schedule-table"
        - A footnotes section: id="fee-schedule-footnotes" as an ordered <ol>
        - Each <li> carries data-feecodes="CA,PC,NC,..." listing exactly which fee
          codes that footnote applies to — this is the authoritative mapping.

        We parse data-feecodes to build a clean manifest Claude can use directly
        to populate footnote_refs for each CSV row without guessing.
        """
        try:
            logger.info("[%s] Fetching HTML for footnotes: %s", self.exchange_id, html_url)
            resp = self.session.get(html_url, timeout=TIMEOUT)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning("[%s] HTML fetch failed: %s", self.exchange_id, exc)
            return ""

        soup = BeautifulSoup(resp.content, "lxml")

        # Locate the footnotes ordered list
        fn_section = (
            soup.find(id="fee-schedule-footnotes")
            or soup.find(class_=re.compile(r"footnote-list", re.I))
        )
        if not fn_section:
            logger.info(
                "[%s] No footnote section found in HTML — page may have no footnotes",
                self.exchange_id,
            )
            return ""

        lines: list[str] = [
            "=== CBOE FEE SCHEDULE FOOTNOTE MANIFEST ===",
            "Each footnote below specifies which CSV fee codes it applies to.",
            "For every CSV row whose Code appears in APPLIES TO CODES, add that",
            "footnote number to footnote_refs and summarise the condition in notes.",
            "",
        ]

        fn_items = fn_section.find_all("li")
        count = 0
        for i, li in enumerate(fn_items, 1):
            raw_codes = li.get("data-feecodes", "")
            codes = [c.strip() for c in raw_codes.split(",") if c.strip()]
            text = li.get_text(separator=" ", strip=True)
            if not text:
                continue
            count += 1
            if codes:
                lines.append(f"FOOTNOTE {i} — APPLIES TO CODES: {', '.join(codes)}")
            else:
                lines.append(f"FOOTNOTE {i} — APPLIES TO: (all codes — see text)")
            lines.append(f"TEXT: {text}")
            lines.append("")

        if count == 0:
            logger.info("[%s] Footnote section found but contained no <li> items", self.exchange_id)
            return ""

        result = "\n".join(lines)
        logger.info("[%s] Extracted %d footnotes from HTML (%d chars)", self.exchange_id, count, len(result))
        return result[:80_000]
