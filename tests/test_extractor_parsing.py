"""Unit tests for all parsing and validation functions in src/extractor/claude.py.

Tests do NOT call the Anthropic API — they exercise the pure parsing logic directly.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from src.extractor.claude import (
    ExtractionFlag,
    FeeRow,
    Footnote,
    _dedup_rows,
    _extract_json,
    _parse_flags,
    _parse_footnotes,
    _parse_rows,
    _to_rate,
    _validate_csv_rows,
    _validate_footnote_coverage,
)

NOW = datetime(2026, 6, 8, 17, 0, 0, tzinfo=timezone.utc)
XID = "edgx"


# ===========================================================================
# _to_rate
# ===========================================================================

class TestToRate:
    def test_none_returns_none(self):
        assert _to_rate(None) is None

    def test_integer(self):
        assert _to_rate(47) == 47.0

    def test_negative_float(self):
        assert _to_rate(-0.47) == -0.47

    def test_string_number(self):
        assert _to_rate("0.10") == 0.10

    def test_string_with_leading_dot(self):
        assert _to_rate(".50") == 0.50

    def test_zero(self):
        assert _to_rate(0) == 0.0

    def test_rounds_to_two_dp(self):
        assert _to_rate(0.123456) == 0.12

    def test_invalid_string_returns_none(self):
        assert _to_rate("not-a-number") is None

    def test_empty_string_returns_none(self):
        assert _to_rate("") is None

    def test_positive_is_rebate(self):
        v = _to_rate(0.28)
        assert v is not None and v > 0

    def test_negative_is_fee(self):
        v = _to_rate(-0.50)
        assert v is not None and v < 0


# ===========================================================================
# _extract_json
# ===========================================================================

class TestExtractJson:
    def test_fenced_json_block(self):
        text = '```json\n{"footnotes": [], "rows": [], "flags": []}\n```'
        result = _extract_json(text)
        assert result is not None
        parsed = json.loads(result)
        assert "rows" in parsed

    def test_fenced_without_language_tag(self):
        text = '```\n{"rows": [1, 2]}\n```'
        result = _extract_json(text)
        assert result is not None

    def test_unfenced_json_in_prose(self):
        text = 'Here is the output:\n{"footnotes": [], "rows": [], "flags": []}'
        result = _extract_json(text)
        assert result is not None
        parsed = json.loads(result)
        assert "footnotes" in parsed

    def test_nested_objects(self):
        payload = {
            "footnotes": [{"ref": "1", "text": "foo", "location": "p1"}],
            "rows": [{"ticker_class": "Penny", "make_rate": 0.10}],
            "flags": [],
        }
        text = json.dumps(payload)
        result = _extract_json(text)
        assert result is not None
        parsed = json.loads(result)
        assert parsed["footnotes"][0]["ref"] == "1"

    def test_returns_none_for_no_json(self):
        assert _extract_json("no json here at all") is None

    def test_returns_none_for_empty_string(self):
        assert _extract_json("") is None

    def test_preamble_text_before_json(self):
        text = "Sure, here is the extraction:\n\n```json\n{\"rows\": []}\n```\n\nLet me know."
        result = _extract_json(text)
        assert result is not None
        assert json.loads(result) == {"rows": []}

    def test_large_nested_json(self):
        """Ensure the balanced-brace extractor handles deeply nested JSON."""
        rows = [
            {"ticker_class": "Penny", "footnote_refs": ["1", "2"], "make_rate": 0.10}
            for _ in range(20)
        ]
        payload = {"footnotes": [{"ref": str(i), "text": f"fn{i}", "location": "p1"} for i in range(5)],
                   "rows": rows, "flags": []}
        text = json.dumps(payload)
        result = _extract_json(text)
        assert result is not None
        parsed = json.loads(result)
        assert len(parsed["rows"]) == 20


# ===========================================================================
# _parse_footnotes
# ===========================================================================

class TestParseFootnotes:
    def test_empty_list(self):
        assert _parse_footnotes([]) == []

    def test_valid_footnotes(self):
        raw = [
            {"ref": "1", "text": "Volume threshold applies.", "location": "Page 2 bottom"},
            {"ref": "*", "text": "PFOF waiver condition.", "location": "Page 1, Table 1"},
        ]
        result = _parse_footnotes(raw)
        assert len(result) == 2
        assert result[0].ref == "1"
        assert result[1].ref == "*"
        assert "PFOF" in result[1].text

    def test_non_dict_items_skipped(self):
        result = _parse_footnotes(["not-a-dict", None, 42])
        assert result == []

    def test_missing_fields_default_to_empty_string(self):
        result = _parse_footnotes([{}])
        assert len(result) == 1
        assert result[0].ref == ""
        assert result[0].text == ""
        assert result[0].location == ""

    def test_ref_coerced_to_string(self):
        result = _parse_footnotes([{"ref": 7, "text": "foo", "location": "p1"}])
        assert result[0].ref == "7"


# ===========================================================================
# _parse_flags
# ===========================================================================

class TestParseFlags:
    def test_empty_list(self):
        assert _parse_flags([]) == []

    def test_warning_and_error(self):
        raw = [
            {"severity": "warning", "location": "Page 1", "issue": "Ambiguous table."},
            {"severity": "error", "location": "Page 3", "issue": "Missing rate."},
        ]
        result = _parse_flags(raw)
        assert len(result) == 2
        assert result[0].severity == "warning"
        assert result[1].severity == "error"

    def test_unknown_severity_defaults_to_warning(self):
        raw = [{"severity": "critical", "location": "p1", "issue": "bad"}]
        result = _parse_flags(raw)
        assert result[0].severity == "warning"

    def test_non_dict_items_skipped(self):
        assert _parse_flags(["oops", None]) == []


# ===========================================================================
# _parse_rows
# ===========================================================================

class TestParseRows:
    def _row(self, **kwargs) -> dict:
        base = {
            "ticker_class": "Penny", "sec_type": "OPT",
            "account_type": "CUST", "trade_type": "Electronic",
            "liq_code": "CA", "make_rate": None, "take_rate": -0.47,
            "auction_init_rate": None, "auction_resp_rate": None, "breakup_rate": None,
            "source_page": "CSV row: CA", "source_section": "Customer adds",
            "footnote_refs": [], "confidence": "medium",
            "confidence_reason": "CBOE CSV omits footnotes.", "notes": None,
        }
        base.update(kwargs)
        return base

    def test_basic_row_parsed(self):
        rows = _parse_rows([self._row()], XID, NOW)
        assert len(rows) == 1
        assert rows[0].liq_code == "CA"
        assert rows[0].take_rate == -0.47
        assert rows[0].confidence == "medium"

    def test_footnote_refs_as_list(self):
        rows = _parse_rows([self._row(footnote_refs=["1", "*"])], XID, NOW)
        assert rows[0].footnote_refs == ["1", "*"]

    def test_footnote_refs_single_value_wrapped(self):
        rows = _parse_rows([self._row(footnote_refs="1")], XID, NOW)
        assert rows[0].footnote_refs == ["1"]

    def test_footnote_refs_none_defaults_to_empty(self):
        rows = _parse_rows([self._row(footnote_refs=None)], XID, NOW)
        assert rows[0].footnote_refs == []

    def test_invalid_sec_type_defaults_to_opt(self):
        rows = _parse_rows([self._row(sec_type="INVALID")], XID, NOW)
        assert rows[0].sec_type == "OPT"

    def test_mleg_sec_type_preserved(self):
        rows = _parse_rows([self._row(sec_type="MLEG")], XID, NOW)
        assert rows[0].sec_type == "MLEG"

    def test_invalid_account_type_defaults_to_cust(self):
        rows = _parse_rows([self._row(account_type="FIRM")], XID, NOW)
        assert rows[0].account_type == "CUST"

    def test_pcust_preserved(self):
        rows = _parse_rows([self._row(account_type="PCUST")], XID, NOW)
        assert rows[0].account_type == "PCUST"

    def test_invalid_trade_type_defaults_to_electronic(self):
        rows = _parse_rows([self._row(trade_type="UNKNOWN")], XID, NOW)
        assert rows[0].trade_type == "Electronic"

    def test_pi_trade_type(self):
        rows = _parse_rows([self._row(trade_type="PI")], XID, NOW)
        assert rows[0].trade_type == "PI"

    def test_solicitation_trade_type(self):
        rows = _parse_rows([self._row(trade_type="Solicitation")], XID, NOW)
        assert rows[0].trade_type == "Solicitation"

    def test_unknown_confidence_defaults_to_medium(self):
        rows = _parse_rows([self._row(confidence="bogus")], XID, NOW)
        assert rows[0].confidence == "medium"

    def test_low_confidence_preserved(self):
        rows = _parse_rows([self._row(confidence="low",
                                      confidence_reason="conflicting footnotes")],
                           XID, NOW)
        assert rows[0].confidence == "low"
        assert rows[0].confidence_reason == "conflicting footnotes"

    def test_all_rate_fields_null(self):
        rows = _parse_rows([self._row(
            make_rate=None, take_rate=None, auction_init_rate=None,
            auction_resp_rate=None, breakup_rate=None,
        )], XID, NOW)
        r = rows[0]
        assert r.make_rate is None
        assert r.take_rate is None
        assert r.auction_init_rate is None
        assert r.auction_resp_rate is None
        assert r.breakup_rate is None

    def test_all_rate_fields_populated(self):
        rows = _parse_rows([self._row(
            make_rate=0.10, take_rate=-0.47,
            auction_init_rate=-0.06, auction_resp_rate=0.50, breakup_rate=-0.05,
        )], XID, NOW)
        r = rows[0]
        assert r.make_rate == 0.10
        assert r.take_rate == -0.47
        assert r.auction_init_rate == -0.06
        assert r.auction_resp_rate == 0.50
        assert r.breakup_rate == -0.05

    def test_rate_string_values_converted(self):
        rows = _parse_rows([self._row(take_rate="-0.47", make_rate="0.10")], XID, NOW)
        assert rows[0].take_rate == -0.47
        assert rows[0].make_rate == 0.10

    def test_non_dict_items_skipped(self):
        rows = _parse_rows(["string", None, 42, {"liq_code": "CA"}], XID, NOW)
        assert all(isinstance(r, FeeRow) for r in rows)

    def test_empty_list(self):
        assert _parse_rows([], XID, NOW) == []

    def test_exchange_id_set(self):
        rows = _parse_rows([self._row()], "bzx", NOW)
        assert rows[0].exchange_id == "bzx"

    def test_extracted_at_set(self):
        rows = _parse_rows([self._row()], XID, NOW)
        assert rows[0].extracted_at == NOW

    def test_multiple_rows_preserved_in_order(self):
        raw = [self._row(liq_code="CA"), self._row(liq_code="NC"), self._row(liq_code="ZA")]
        rows = _parse_rows(raw, XID, NOW)
        assert [r.liq_code for r in rows] == ["CA", "NC", "ZA"]

    def test_as_dict_round_trips_footnote_refs(self):
        rows = _parse_rows([self._row(footnote_refs=["1", "*"])], XID, NOW)
        d = rows[0].as_dict()
        # footnote_refs is JSON-encoded in the dict (for SQLite storage)
        assert json.loads(d["footnote_refs"]) == ["1", "*"]


# ===========================================================================
# _dedup_rows
# ===========================================================================

class TestDedupRows:
    def _make(self, liq_code: str, take_rate: float = -0.47) -> FeeRow:
        from tests.conftest import make_row
        return make_row(liq_code=liq_code, take_rate=take_rate)

    def test_no_duplicates_unchanged(self):
        rows = [self._make("CA"), self._make("NC"), self._make("ZA")]
        deduped, flags = _dedup_rows(rows, XID)
        assert len(deduped) == 3
        assert flags == []

    def test_exact_duplicate_removed(self):
        ca = self._make("CA")
        rows = [ca, ca]
        deduped, flags = _dedup_rows(rows, XID)
        assert len(deduped) == 1
        assert len(flags) == 1
        assert "duplicate" in flags[0].issue.lower()

    def test_keeps_first_occurrence(self):
        first = self._make("CA", take_rate=-0.47)
        second = self._make("CA", take_rate=-0.50)  # same key, different value
        deduped, _ = _dedup_rows([first, second], XID)
        assert deduped[0].take_rate == -0.47

    def test_multiple_dupes_all_removed(self):
        ca = self._make("CA")
        rows = [ca, ca, ca, self._make("NC")]
        deduped, flags = _dedup_rows(rows, XID)
        assert len(deduped) == 2
        assert len(flags) == 1
        assert "2" in flags[0].issue  # "2 duplicate row(s) removed"

    def test_null_liq_codes_considered_same_key(self):
        from tests.conftest import make_row
        r1 = make_row(liq_code=None)
        r2 = make_row(liq_code=None)
        deduped, flags = _dedup_rows([r1, r2], XID)
        assert len(deduped) == 1
        assert flags  # should flag the duplicate

    def test_empty_input(self):
        deduped, flags = _dedup_rows([], XID)
        assert deduped == []
        assert flags == []

    def test_flag_severity_is_warning(self):
        ca = self._make("CA")
        _, flags = _dedup_rows([ca, ca], XID)
        assert flags[0].severity == "warning"


# ===========================================================================
# _validate_csv_rows
# ===========================================================================

class TestValidateCsvRows:
    def test_all_liq_codes_present_no_flags(self):
        from tests.conftest import make_row
        rows = [make_row(liq_code="CA"), make_row(liq_code="NC")]
        assert _validate_csv_rows(rows, XID) == []

    def test_null_liq_code_produces_error_flag(self):
        from tests.conftest import make_row
        rows = [make_row(liq_code=None)]
        flags = _validate_csv_rows(rows, XID)
        assert len(flags) == 1
        assert flags[0].severity == "error"
        assert "null liq_code" in flags[0].issue

    def test_count_in_flag_message(self):
        from tests.conftest import make_row
        rows = [make_row(liq_code=None), make_row(liq_code=None), make_row(liq_code="CA")]
        flags = _validate_csv_rows(rows, XID)
        assert len(flags) == 1
        assert "2" in flags[0].issue

    def test_empty_rows_no_flags(self):
        assert _validate_csv_rows([], XID) == []


# ===========================================================================
# _validate_footnote_coverage
# ===========================================================================

class TestValidateFootnoteCoverage:
    def _fn(self) -> Footnote:
        return Footnote(ref="1", text="Volume threshold.", location="Page 1")

    def test_no_footnotes_in_doc_no_flag(self):
        from tests.conftest import make_row
        rows = [make_row(confidence="high", footnote_refs=[])]
        flags = _validate_footnote_coverage(rows, [], XID)
        assert flags == []

    def test_high_conf_empty_refs_with_footnotes_flags(self):
        from tests.conftest import make_row
        rows = [make_row(confidence="high", footnote_refs=[])]
        flags = _validate_footnote_coverage(rows, [self._fn()], XID)
        assert len(flags) == 1
        assert flags[0].severity == "warning"
        assert "high confidence" in flags[0].issue.lower()

    def test_medium_conf_empty_refs_does_not_flag(self):
        from tests.conftest import make_row
        rows = [make_row(confidence="medium", footnote_refs=[])]
        flags = _validate_footnote_coverage(rows, [self._fn()], XID)
        assert flags == []

    def test_high_conf_with_refs_does_not_flag(self):
        from tests.conftest import make_row
        rows = [make_row(confidence="high", footnote_refs=["1"])]
        flags = _validate_footnote_coverage(rows, [self._fn()], XID)
        assert flags == []

    def test_mixed_rows_counts_only_suspicious(self):
        from tests.conftest import make_row
        rows = [
            make_row(liq_code="CA", confidence="high", footnote_refs=[]),   # suspicious
            make_row(liq_code="NC", confidence="high", footnote_refs=[]),   # suspicious
            make_row(liq_code="PC", confidence="high", footnote_refs=["1"]), # fine
            make_row(liq_code="BC", confidence="medium", footnote_refs=[]), # fine
        ]
        flags = _validate_footnote_coverage(rows, [self._fn()], XID)
        assert len(flags) == 1
        assert "2" in flags[0].issue  # 2 suspicious rows

    def test_empty_rows_no_flag(self):
        flags = _validate_footnote_coverage([], [self._fn()], XID)
        assert flags == []


# ===========================================================================
# FeeRow helper methods
# ===========================================================================

class TestFeeRowHelpers:
    def test_needs_review_high_is_false(self):
        from tests.conftest import make_row
        assert make_row(confidence="high").needs_review is False

    def test_needs_review_medium_is_true(self):
        from tests.conftest import make_row
        assert make_row(confidence="medium").needs_review is True

    def test_needs_review_low_is_true(self):
        from tests.conftest import make_row
        assert make_row(confidence="low").needs_review is True

    def test_citation_all_fields(self):
        from tests.conftest import make_row
        r = make_row(source_page="Page 2", source_section="Table 1", footnote_refs=["1"])
        c = r.citation()
        assert "Page 2" in c
        assert "Table 1" in c
        assert "fn. 1" in c

    def test_citation_no_fields(self):
        from tests.conftest import make_row
        r = make_row(source_page=None, source_section=None, footnote_refs=[])
        assert r.citation() == "unknown"
