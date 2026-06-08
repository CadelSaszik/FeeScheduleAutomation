"""Mock extractor for testing the pipeline without an Anthropic API key.

Returns a small set of realistic-looking fee rows so the fetch → diff → alert
chain can be exercised end-to-end without hitting the Claude API.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timezone

from .claude import ExtractionResult, FeeRow

logger = logging.getLogger(__name__)

# Realistic base rates per operator family (make, take)
_BASE_RATES = {
    "cboe":   (0.10, -0.47),
    "nasdaq": (0.12, -0.49),
    "nyse":   (0.09, -0.45),
    "miax":   (0.11, -0.48),
    "box":    (0.00, -0.50),
    "memx":   (0.10, -0.47),
}

_TICKER_CLASSES = ["Penny", "Non-Penny"]
_TRADE_TYPES = ["Electronic", "PI", "Solicitation"]
_SEC_TYPES = ["OPT", "MLEG"]
_ACCOUNT_TYPES = ["CUST", "PCUST"]


class MockExtractor:
    """Drop-in replacement for ClaudeExtractor during testing."""

    def __init__(self, jitter: bool = False):
        """
        Args:
            jitter: If True, randomly nudge a rate so the diff engine
                    sees a change. Useful for testing alert delivery.
        """
        self.jitter = jitter

    def extract(
        self,
        exchange_id: str,
        operator: str,
        exchange_name: str,
        fee_text: str,
        content_type: str = "text",
        supplemental_text: str = "",
    ) -> ExtractionResult:
        extracted_at = datetime.now(tz=timezone.utc)
        base_make, base_take = _BASE_RATES.get(operator, (0.10, -0.47))
        rows: list[FeeRow] = []

        for ticker_class in _TICKER_CLASSES:
            for account_type in _ACCOUNT_TYPES:
                for trade_type in _TRADE_TYPES:
                    for sec_type in _SEC_TYPES:
                        make = round(base_make + (0.02 if ticker_class == "Non-Penny" else 0.0), 2)
                        take = round(base_take - (0.03 if ticker_class == "Non-Penny" else 0.0), 2)
                        if account_type == "PCUST":
                            make = round(make - 0.05, 2)
                            take = round(take - 0.02, 2)

                        auction_init = round(make + 0.05, 2) if trade_type == "PI" else None
                        auction_resp = round(take + 0.10, 2) if trade_type == "PI" else None
                        breakup = round(-0.05, 2) if trade_type in ("PI", "Solicitation") else None

                        if self.jitter and random.random() < 0.05:
                            take = round(take - 0.03, 2)

                        rows.append(FeeRow(
                            exchange_id=exchange_id,
                            extracted_at=extracted_at,
                            ticker_class=ticker_class,
                            sec_type=sec_type,
                            account_type=account_type,
                            trade_type=trade_type,
                            liq_code=None,
                            make_rate=make,
                            take_rate=take,
                            auction_init_rate=auction_init,
                            auction_resp_rate=auction_resp,
                            breakup_rate=breakup,
                            source_page="[mock]",
                            source_section="[mock]",
                            footnote_refs=[],
                            confidence="high",
                            confidence_reason=None,
                            notes="[MOCK DATA — not from real fee schedule]",
                        ))

        logger.info("[%s] Mock extractor produced %d rows", exchange_id, len(rows))
        return ExtractionResult(
            exchange_id=exchange_id,
            operator=operator,
            extracted_at=extracted_at,
            rows=rows,
            raw_response="[mock]",
            input_tokens=0,
            output_tokens=0,
        )
