"""NASDAQ family fetcher: NOM, BX, PHLX, ISE, Gemini, Mercury.

NASDAQ fee pages are HTML. We attempt to fetch them directly; if the
page requires JS rendering the raw HTML still contains the table data
as server-side HTML in most cases.
"""

from __future__ import annotations

import logging

from bs4 import BeautifulSoup

from .base import BaseFetcher, FetchResult

logger = logging.getLogger(__name__)


class NasdaqFetcher(BaseFetcher):
    def extract_text(self, result: FetchResult) -> str:
        if not result.ok:
            return ""
        return _html_to_text(result.raw_bytes, result.exchange_id)


def _html_to_text(data: bytes, exchange_id: str) -> str:
    try:
        soup = BeautifulSoup(data, "lxml")

        # Remove nav, footer, script, style noise
        for tag in soup.find_all(["script", "style", "nav", "footer", "header"]):
            tag.decompose()

        # Render tables explicitly so the AI can read them cleanly
        parts = []
        for table in soup.find_all("table"):
            rows = []
            for tr in table.find_all("tr"):
                cells = [td.get_text(strip=True) for td in tr.find_all(["th", "td"])]
                rows.append("\t".join(cells))
            parts.append("[TABLE]\n" + "\n".join(rows) + "\n[/TABLE]")

        # Also grab remaining text
        body_text = soup.get_text(separator="\n", strip=True)
        parts.insert(0, body_text)
        return "\n\n".join(parts)
    except Exception as exc:
        logger.error("[%s] HTML extraction error: %s", exchange_id, exc)
        return ""
