"""Tests for error paths and previously uncovered helpers in claude.py.

Covers:
  - Footnote.as_dict() and ExtractionFlag.as_dict()          (lines 30, 40)
  - anthropic.APIError handler                               (lines 204-206)
  - Unexpected exception handler                             (lines 207-209)
  - No JSON found in Claude response                         (lines 225-226)
  - JSONDecodeError in _parse_response                       (lines 230-232)
  - Malformed row skipped in _parse_rows                     (lines 317-318)
  - _extract_json returns None for unclosed brace            (line 341)
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
# No JSON in response (lines 225-226)
# ===========================================================================

class TestNoJsonInResponse:
    def _run_with_text(self, response_text: str):
        msg = MagicMock()
        msg.content[0].text = response_text
        msg.usage.input_tokens = 100
        msg.usage.output_tokens = 50
        with patch("anthropic.Anthropic") as MockClient:
            MockClient.return_value.messages.create.return_value = msg
            extractor = ClaudeExtractor()
            return extractor.extract("edgx", "cboe", "CBOE EDGX", "some fee data")

    def test_prose_only_returns_empty_rows(self):
        result = self._run_with_text("I found some fees in the document.")
        assert result.rows == []
        assert result.footnotes == []

    def test_prose_only_result_has_no_error(self):
        # No API error — just an unhelpful response from Claude
        result = self._run_with_text("No fees found.")
        assert result.error is None

    def test_partial_json_returns_empty(self):
        result = self._run_with_text("Here is part of the data: {incomplete")
        assert result.rows == []


# ===========================================================================
# JSONDecodeError (lines 230-232)
# ===========================================================================

class TestJsonDecodeError:
    def _run_with_json_text(self, raw_text: str):
        msg = MagicMock()
        msg.content[0].text = raw_text
        msg.usage.input_tokens = 100
        msg.usage.output_tokens = 50
        with patch("anthropic.Anthropic") as MockClient:
            MockClient.return_value.messages.create.return_value = msg
            extractor = ClaudeExtractor()
            return extractor.extract("edgx", "cboe", "CBOE EDGX", "some fee data")

    def test_invalid_json_returns_empty_rows(self):
        # Looks like JSON but is invalid (missing closing quote)
        result = self._run_with_json_text('{"rows": [{"ticker_class": "Penny]}}')
        assert result.rows == []

    def test_invalid_json_no_error_field(self):
        result = self._run_with_json_text('{"rows": [broken json here}')
        assert result.error is None  # not an API error, just a parse failure

    def test_truncated_json_returns_empty(self):
        result = self._run_with_json_text('{"rows": [{"ticker_class": "Penny", "sec_type":')
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
