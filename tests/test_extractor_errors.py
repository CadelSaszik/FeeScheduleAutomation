"""Tests for error paths and helpers in claude.py.

Covers:
  - Footnote.as_dict() and ExtractionFlag.as_dict()
  - anthropic.APIError handler (fires on Pass 1 for text content)
  - Unexpected exception handler
  - Malformed row skipped in _parse_rows
  - _extract_json returns None for unclosed brace (utility function, kept for debugging)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.extractor.claude import (
    ClaudeExtractor,
    ExtractionFlag,
    FeeRow,
    Footnote,
    _extract_json,
    _parse_rows,
)

NOW = datetime(2026, 6, 8, 17, 0, 0, tzinfo=timezone.utc)
XID = "edgx"


# ===========================================================================
# Footnote.as_dict() and ExtractionFlag.as_dict()
# ===========================================================================

class TestAsDict:
    def test_footnote_as_dict_keys(self):
        fn = Footnote(ref="1", text="Volume threshold.", location="Page 2 bottom")
        d = fn.as_dict()
        assert d == {"ref": "1", "text": "Volume threshold.", "location": "Page 2 bottom"}

    def test_footnote_as_dict_unicode_ref(self):
        fn = Footnote(ref="†", text="Dagger footnote.", location="Page 1")
        d = fn.as_dict()
        assert d["ref"] == "†"

    def test_extraction_flag_as_dict_keys(self):
        flag = ExtractionFlag(severity="warning", location="Page 3", issue="Ambiguous table.")
        d = flag.as_dict()
        assert d == {"severity": "warning", "location": "Page 3", "issue": "Ambiguous table."}

    def test_extraction_flag_error_severity(self):
        flag = ExtractionFlag(severity="error", location="Page 1", issue="Missing rate.")
        d = flag.as_dict()
        assert d["severity"] == "error"


# ===========================================================================
# _extract_json — unclosed brace (line 341)
# ===========================================================================

class TestExtractJsonEdgeCases:
    def test_unclosed_brace_returns_none(self):
        assert _extract_json("{unclosed") is None

    def test_unclosed_nested_returns_none(self):
        assert _extract_json('{"rows": [{"a": 1}') is None

    def test_empty_object_valid(self):
        result = _extract_json("{}")
        assert result == "{}"

    def test_deeply_nested_closed_valid(self):
        payload = {"a": {"b": {"c": 1}}}
        result = _extract_json(json.dumps(payload))
        assert result is not None
        assert json.loads(result) == payload


# ===========================================================================
# APIError and unexpected exception handlers (lines 204-209)
# ===========================================================================

class TestClaudeExtractorApiErrors:
    def _make_extractor(self):
        with patch("anthropic.Anthropic"):
            return ClaudeExtractor()

    def test_api_error_produces_error_result(self):
        import anthropic
        extractor = self._make_extractor()
        extractor.client.messages.create.side_effect = anthropic.APIError(
            message="rate limit",
            request=MagicMock(),
            body={"error": {"type": "rate_limit_error"}},
        )
        result = extractor.extract("edgx", "cboe", "CBOE EDGX", "some fee data")
        assert not result.ok
        assert "Anthropic API error" in result.error

    def test_api_error_result_has_no_rows(self):
        import anthropic
        extractor = self._make_extractor()
        extractor.client.messages.create.side_effect = anthropic.APIError(
            message="server error",
            request=MagicMock(),
            body={"error": {"type": "api_error"}},
        )
        result = extractor.extract("edgx", "cboe", "CBOE EDGX", "some fee data")
        assert result.rows == []

    def test_unexpected_exception_produces_error_result(self):
        extractor = self._make_extractor()
        extractor.client.messages.create.side_effect = RuntimeError("connection reset")
        result = extractor.extract("edgx", "cboe", "CBOE EDGX", "some fee data")
        assert not result.ok
        assert "Unexpected extraction error" in result.error

    def test_unexpected_exception_has_no_rows(self):
        extractor = self._make_extractor()
        extractor.client.messages.create.side_effect = ValueError("unexpected value error")
        result = extractor.extract("edgx", "cboe", "CBOE EDGX", "some fee data")
        assert result.rows == []


# ===========================================================================
# Malformed row skipped (lines 317-318)
# ===========================================================================

class TestMalformedRowSkipped:
    def test_row_raising_on_construction_is_skipped(self):
        """Patch FeeRow so that it raises on the first call, then works normally."""
        good_raw = {
            "ticker_class": "Penny", "sec_type": "OPT",
            "account_type": "CUST", "trade_type": "Electronic",
            "liq_code": "CA", "make_rate": None, "take_rate": -0.01,
            "auction_init_rate": None, "auction_resp_rate": None, "breakup_rate": None,
            "source_page": "p1", "source_section": "Table 1",
            "footnote_refs": [], "confidence": "medium",
            "confidence_reason": "test", "notes": None,
        }

        call_count = 0
        original_init = FeeRow.__init__

        def patched_init(self, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ValueError("simulated construction failure")
            return original_init(self, *args, **kwargs)

        with patch.object(FeeRow, "__init__", patched_init):
            rows = _parse_rows([good_raw, good_raw], XID, NOW)

        # First row raised and was skipped; second succeeded
        assert len(rows) == 1

    def test_str_raising_element_in_footnote_refs_is_skipped(self):
        """A footnote_refs element whose __str__ raises triggers the exception handler.

        _parse_rows iterates fn_refs with [str(r).strip() for r in fn_refs].
        A list element that raises on str() bubbles up to the outer try/except.
        """
        class RaisesOnStr:
            def __str__(self):
                raise ValueError("str conversion failed")

        raw = {
            "ticker_class": "Penny", "sec_type": "OPT",
            "account_type": "CUST", "trade_type": "Electronic",
            "liq_code": "CA", "make_rate": None, "take_rate": -0.01,
            "auction_init_rate": None, "auction_resp_rate": None, "breakup_rate": None,
            "source_page": "p1", "source_section": "s1",
            "footnote_refs": [RaisesOnStr()],   # list element raises on str()
            "confidence": "medium",
            "confidence_reason": "test", "notes": None,
        }
        rows = _parse_rows([raw], XID, NOW)
        assert rows == []
