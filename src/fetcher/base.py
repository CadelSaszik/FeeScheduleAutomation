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

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; FeeScheduleBot/1.0; "
        "+https://watershedtech.us)"
    )
}


@dataclass
class FetchResult:
    exchange_id: str
    operator: str
    url: str
    fetched_at: datetime
    content_type: str          # "pdf" | "html"
    raw_bytes: bytes
    file_path: Path
    http_status: int
    error: Optional[str] = None

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

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def fetch(self) -> FetchResult:
        """Download the fee schedule and persist it to disk."""
        fetched_at = datetime.now(tz=timezone.utc)
        dest = self._dest_path(fetched_at)

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                logger.info(
                    "[%s] Fetching %s (attempt %d/%d)",
                    self.exchange_id, self.fee_url, attempt, MAX_RETRIES
                )
                raw, status = self._download(self.fee_url)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(raw)
                logger.info("[%s] Saved → %s (%d bytes)", self.exchange_id, dest, len(raw))
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
            except Exception as exc:
                logger.warning("[%s] Attempt %d failed: %s", self.exchange_id, attempt, exc)
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

    def _download(self, url: str) -> tuple[bytes, int]:
        resp = self.session.get(url, timeout=TIMEOUT, allow_redirects=True)
        resp.raise_for_status()
        return resp.content, resp.status_code

    def _dest_path(self, ts: datetime) -> Path:
        stamp = ts.strftime("%Y%m%dT%H%M%SZ")
        ext = "pdf" if self.schedule_type == "pdf" else "html"
        return RAW_DIR / self.exchange_id / f"{self.exchange_id}_{stamp}.{ext}"

    # Subclasses may override to handle JS-rendered pages etc.
    @abstractmethod
    def extract_text(self, result: FetchResult) -> str:
        """Convert raw bytes to plain text for the AI extraction step."""
