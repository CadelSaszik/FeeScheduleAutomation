"""Base fetcher — download a fee schedule and return raw bytes + metadata."""

from __future__ import annotations

import logging
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

RAW_DIR = Path(os.getenv("RAW_DATA_DIR", "data/raw"))
TIMEOUT = int(os.getenv("REQUEST_TIMEOUT_SEC", "30"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))

# Browser-realistic headers. The User-Agent matches a current Chrome on Windows.
# Referer is set per-operator in each subclass's _download() override.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "application/pdf,image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",  # omit 'br' — requests can't decode Brotli without the brotli package
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "DNT": "1",
}

# Landing page that should appear in Referer for each operator's CDN.
# These are the pages a real user would navigate from to reach the PDF link.
OPERATOR_REFERERS = {
    "cboe":   "https://www.cboe.com/us/options/membership/fee_schedule/",
    "nasdaq": "https://listingcenter.nasdaq.com/rulebook/nasdaq/rules/",
    "nyse":   "https://www.nyse.com/markets/options-fees",
    "miax":   "https://www.miaxoptions.com/fee-schedules",
    "box":    "https://boxoptions.com/trading/fee-schedule/",
    "memx":   "https://info.memxtrading.com/fee-schedules/",
}


@dataclass
class FetchResult:
    exchange_id: str
    operator: str
    url: str
    fetched_at: datetime
    content_type: str          # "pdf" | "html" | "csv"
    raw_bytes: bytes
    file_path: Path
    http_status: int
    error: Optional[str] = None
    supplemental_text: str = ""  # footnote content from a secondary source (e.g. HTML page alongside CSV)

    @property
    def ok(self) -> bool:
        return self.error is None and self.http_status == 200


class BaseFetcher(ABC):
    def __init__(self, exchange_cfg: dict):
        self.cfg = exchange_cfg
        self.exchange_id: str = exchange_cfg["id"]
        self.operator: str = exchange_cfg["operator"]
        self.fee_url: str = exchange_cfg["fee_url"]
        self.schedule_type: str = exchange_cfg["schedule_type"]
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        # Set operator-specific Referer so requests look like they came from
        # navigating the exchange's own site rather than hitting the URL cold.
        referer = OPERATOR_REFERERS.get(self.operator, "")
        if referer:
            self.session.headers["Referer"] = referer

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def fetch(self) -> FetchResult:
        """Download the fee schedule and persist it to disk.

        Before making any HTTP request, checks for a manually placed file at
        data/raw/<exchange_id>/manual.<ext> (pdf, html, or csv).  If found,
        that file is used as-is — no HTTP request is made.  Drop a manually
        downloaded file there to bypass any blocked or dated URL.
        """
        manual = self._find_manual_file()
        if manual is not None:
            return self._load_manual_file(manual)

        fetched_at = datetime.now(tz=timezone.utc)
        dest = self._dest_path(fetched_at)

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                logger.info(
                    "[%s] Fetching %s (attempt %d/%d)",
                    self.exchange_id, self.fee_url, attempt, MAX_RETRIES,
                )
                raw, status = self._download(self.fee_url)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(raw)
                logger.info(
                    "[%s] Saved -> %s (%d bytes)", self.exchange_id, dest, len(raw)
                )
                return FetchResult(
                    exchange_id=self.exchange_id,
                    operator=self.operator,
                    url=self.fee_url,
                    fetched_at=fetched_at,
                    content_type=self.schedule_type,
                    raw_bytes=raw,
                    file_path=dest,
                    http_status=status,
                )
            except requests.HTTPError as exc:
                status_code = exc.response.status_code if exc.response is not None else 0
                if status_code == 403:
                    # 403 is not a transient error — retrying won't help.
                    # Log clearly and bail rather than burning all retries.
                    logger.error(
                        "[%s] 403 Forbidden fetching %s — the site is blocking automated "
                        "requests. Check that the URL in exchanges.yaml is current and "
                        "accessible in a browser. If the URL is correct, the exchange may "
                        "require a session cookie; try downloading the file manually and "
                        "placing it at %s.",
                        self.exchange_id, self.fee_url, dest,
                    )
                    return FetchResult(
                        exchange_id=self.exchange_id,
                        operator=self.operator,
                        url=self.fee_url,
                        fetched_at=fetched_at,
                        content_type=self.schedule_type,
                        raw_bytes=b"",
                        file_path=dest,
                        http_status=403,
                        error=(
                            f"403 Forbidden — {self.fee_url}\n"
                            f"Open that URL in a browser to confirm it's valid, then "
                            f"download the file and place it at {dest} for a manual run."
                        ),
                    )
                logger.warning(
                    "[%s] Attempt %d HTTP %s: %s",
                    self.exchange_id, attempt, status_code, exc,
                )
            except Exception as exc:
                logger.warning(
                    "[%s] Attempt %d failed: %s", self.exchange_id, attempt, exc
                )

            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)

        return FetchResult(
            exchange_id=self.exchange_id,
            operator=self.operator,
            url=self.fee_url,
            fetched_at=fetched_at,
            content_type=self.schedule_type,
            raw_bytes=b"",
            file_path=dest,
            http_status=0,
            error=f"Failed after {MAX_RETRIES} attempts",
        )

    # ------------------------------------------------------------------
    # Overridable helpers
    # ------------------------------------------------------------------

    def _find_manual_file(self) -> Optional[Path]:
        """Return path to a manually placed override file, or None."""
        manual_dir = RAW_DIR / self.exchange_id
        for ext in ("pdf", "html", "csv"):
            candidate = manual_dir / f"manual.{ext}"
            if candidate.exists():
                logger.info(
                    "[%s] Manual override file found: %s — skipping HTTP fetch",
                    self.exchange_id, candidate,
                )
                return candidate
        return None

    def _load_manual_file(self, path: Path) -> FetchResult:
        ext = path.suffix.lstrip(".")
        content_type = {"pdf": "pdf", "html": "html", "csv": "csv"}.get(ext, self.schedule_type)
        return FetchResult(
            exchange_id=self.exchange_id,
            operator=self.operator,
            url=f"file://{path}",
            fetched_at=datetime.now(tz=timezone.utc),
            content_type=content_type,
            raw_bytes=path.read_bytes(),
            file_path=path,
            http_status=200,
        )

    def _download(self, url: str) -> tuple[bytes, int]:
        resp = self.session.get(url, timeout=TIMEOUT, allow_redirects=True)
        resp.raise_for_status()
        return resp.content, resp.status_code

    def _dest_path(self, ts: datetime) -> Path:
        stamp = ts.strftime("%Y%m%dT%H%M%SZ")
        ext = "pdf" if self.schedule_type == "pdf" else "html"
        return RAW_DIR / self.exchange_id / f"{self.exchange_id}_{stamp}.{ext}"

    @abstractmethod
    def extract_text(self, result: FetchResult) -> str:
        """Convert raw bytes to plain text for the AI extraction step."""
