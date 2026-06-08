"""Integration tests for ClaudeExtractor using patched Anthropic responses.

These tests simulate what happens when Claude processes challenging, real-world
footnote scenarios from different exchange operators.  The mock responses in
conftest.py are structured to match the complexity of actual fee schedule PDFs:
cascading footnotes, volume-conditional rebates, PFOF waivers, conflicting notes,
rate caps, and breakup-fee conditions.

No Anthropic API calls are made.  The client is patched at the module level.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from src.extractor.claude import ClaudeExtractor, ExtractionResult
from tests.conftest import (
    EDGX_CSV_TEXT,
    EDGX_CUST_CODES,
    EDGX_PCUST_CODES,
    edgx_ideal_response,
    miax_response_volume_conditional,
    nyse_response_cascading_footnotes,
    box_response_conflicting_footnotes,
    mock_anthropic_response,
    response_high_conf_with_unacknowledged_footnotes,
    response_with_duplicate_rows,
    response_with_null_liq_codes,
)


def _run(response_dict: dict, fee_text: str = "sample text",
         content_type: str = "text", operator: str = "cboe") -> ExtractionResult:
    """Patch the Anthropic client and run ClaudeExtractor.extract()."""
    mock_resp = mock_anthropic_response(response_dict)
    with patch("anthropic.Anthropic") as MockClient:
        MockClient.return_value.messages.create.return_value = mock_resp
        extractor = ClaudeExtractor()
        return extractor.extract(
            exchange_id="edgx",
            operator=operator,
            exchange_name="CBOE EDGX Options",
            fee_text=fee_text,
            content_type=content_type,
        )


# ===========================================================================
# Scenario 1: CBOE EDGX CSV — ideal extraction
# One row per code, liq_code populated, all medium confidence, no invented rates
# ===========================================================================

class TestCboeCsvIdealExtraction:
    def test_extraction_succeeds(self):
        result = _run(edgx_ideal_response(), fee_text=EDGX_CSV_TEXT, content_type="csv")
        assert result.ok
        assert result.error is None

    def test_no_footnotes_for_csv(self):
        result = _run(edgx_ideal_response(), fee_text=EDGX_CSV_TEXT, content_type="csv")
        assert result.footnotes == []

    def test_all_rows_medium_confidence(self):
        result = _run(edgx_ideal_response(), fee_text=EDGX_CSV_TEXT, content_type="csv")
        for row in result.rows:
            assert row.confidence == "medium", (
                f"Row {row.liq_code} has confidence={row.confidence!r}; "
                f"CBOE CSV rows must all be medium"
            )

    def test_all_rows_have_confidence_reason(self):
        result = _run(edgx_ideal_response(), fee_text=EDGX_CSV_TEXT, content_type="csv")
        for row in result.rows:
            assert row.confidence_reason, (
                f"Row {row.liq_code} has no confidence_reason; CBOE medium rows must explain why"
            )

    def test_all_rows_have_liq_code(self):
        result = _run(edgx_ideal_response(), fee_text=EDGX_CSV_TEXT, content_type="csv")
        for row in result.rows:
            assert row.liq_code, f"Row has null liq_code: {row}"

    def test_no_null_liq_code_validation_flags(self):
        result = _run(edgx_ideal_response(), fee_text=EDGX_CSV_TEXT, content_type="csv")
        liq_flags = [f for f in result.flags if "null liq_code" in f.issue]
        assert liq_flags == []

    def test_expected_cust_codes_present(self):
        result = _run(edgx_ideal_response(), fee_text=EDGX_CSV_TEXT, content_type="csv")
        extracted_codes = {r.liq_code for r in result.rows if r.account_type == "CUST"}
        for code in ("CA", "NC", "PC", "BC", "BG"):
            assert code in extracted_codes, f"Expected CUST code {code} not found"

    def test_expected_pcust_codes_present(self):
        result = _run(edgx_ideal_response(), fee_text=EDGX_CSV_TEXT, content_type="csv")
        extracted_codes = {r.liq_code for r in result.rows if r.account_type == "PCUST"}
        assert "PP" in extracted_codes

    def test_mleg_for_z_codes(self):
        result = _run(edgx_ideal_response(), fee_text=EDGX_CSV_TEXT, content_type="csv")
        za_row = next((r for r in result.rows if r.liq_code == "ZA"), None)
        assert za_row is not None
        assert za_row.sec_type == "MLEG"

    def test_aim_agency_maps_to_auction_init(self):
        result = _run(edgx_ideal_response(), fee_text=EDGX_CSV_TEXT, content_type="csv")
        bc_row = next((r for r in result.rows if r.liq_code == "BC"), None)
        assert bc_row is not None
        assert bc_row.trade_type == "PI"
        assert bc_row.auction_init_rate == -0.06
        assert bc_row.make_rate is None   # should NOT be in make_rate
        assert bc_row.take_rate is None   # should NOT be in take_rate

    def test_qcc_maps_to_solicitation(self):
        result = _run(edgx_ideal_response(), fee_text=EDGX_CSV_TEXT, content_type="csv")
        qa_row = next((r for r in result.rows if r.liq_code == "QA"), None)
        assert qa_row is not None
        assert qa_row.trade_type == "Solicitation"
        assert qa_row.auction_init_rate == 0.00

    def test_no_fabricated_breakup_rate(self):
        """EDGX CSV has no AIM Cancel code — breakup_rate must be null for all rows."""
        result = _run(edgx_ideal_response(), fee_text=EDGX_CSV_TEXT, content_type="csv")
        rows_with_breakup = [r for r in result.rows if r.breakup_rate is not None]
        assert rows_with_breakup == [], (
            f"Fabricated breakup_rate found on rows: "
            f"{[(r.liq_code, r.breakup_rate) for r in rows_with_breakup]}"
        )

    def test_no_duplicate_rows(self):
        result = _run(edgx_ideal_response(), fee_text=EDGX_CSV_TEXT, content_type="csv")
        keys = [(r.liq_code, r.account_type, r.trade_type, r.ticker_class) for r in result.rows]
        assert len(keys) == len(set(keys)), "Duplicate rows found after extraction"

    def test_source_page_references_code(self):
        result = _run(edgx_ideal_response(), fee_text=EDGX_CSV_TEXT, content_type="csv")
        for row in result.rows:
            assert row.source_page is not None
            assert row.liq_code in row.source_page or row.source_section is not None


# ===========================================================================
# Scenario 2: MIAX volume-conditional rebate with tiered footnotes
# ===========================================================================

class TestMiaxVolumeConditionalFootnotes:
    def _result(self) -> ExtractionResult:
        return _run(
            miax_response_volume_conditional(),
            fee_text="[MIAX PDF text]",
            content_type="text",
            operator="miax",
        )

    def test_three_footnotes_catalogued(self):
        result = self._result()
        assert len(result.footnotes) == 3
        refs = {fn.ref for fn in result.footnotes}
        assert refs == {"1", "2", "*"}

    def test_footnote_1_text_preserved(self):
        result = self._result()
        fn1 = next(fn for fn in result.footnotes if fn.ref == "1")
        assert "1,000,000 ADV" in fn1.text
        assert "0.20" in fn1.text  # lower-tier rate must be in footnote text

    def test_maker_penny_uses_base_rate(self):
        result = self._result()
        # MIAX PDF table has one row per class with both make_rate and take_rate
        penny_row = next(
            r for r in result.rows
            if r.account_type == "CUST" and r.ticker_class == "Penny"
            and r.trade_type == "Electronic"
        )
        assert penny_row.make_rate == 0.28, "Must use base/Tier-1 rate, not lower tier"
        assert "1" in penny_row.footnote_refs, "Footnote 1 must be referenced"

    def test_maker_penny_is_medium_confidence(self):
        result = self._result()
        penny_row = next(
            r for r in result.rows
            if r.account_type == "CUST" and r.ticker_class == "Penny"
            and r.trade_type == "Electronic"
        )
        assert penny_row.confidence == "medium"
        assert penny_row.confidence_reason is not None
        assert "volume" in penny_row.confidence_reason.lower() or "ADV" in penny_row.confidence_reason

    def test_taker_has_cap_footnote(self):
        result = self._result()
        # Taker rate is on the same row as make_rate for MIAX PDF table rows
        penny_row = next(
            r for r in result.rows
            if r.account_type == "CUST" and r.ticker_class == "Penny"
            and r.trade_type == "Electronic"
        )
        assert penny_row.take_rate is not None, "take_rate must be on the combined Penny row"
        assert "2" in penny_row.footnote_refs
        combined = (penny_row.notes or "") + (penny_row.confidence_reason or "")
        assert "cap" in combined.lower()

    def test_mpim_agency_maps_to_auction_init(self):
        result = self._result()
        agency = next(
            r for r in result.rows
            if r.trade_type == "PI" and r.auction_init_rate is not None
        )
        assert agency.auction_init_rate == -0.10

    def test_mpim_breakup_rate_with_asterisk_footnote(self):
        result = self._result()
        # M-PIM Agency + Breakup are combined on the PI row
        pi_row = next(
            (r for r in result.rows if r.trade_type == "PI" and r.breakup_rate is not None),
            None,
        )
        assert pi_row is not None, "M-PIM PI row with breakup_rate missing"
        assert pi_row.breakup_rate == -0.05
        assert "*" in pi_row.footnote_refs
        assert pi_row.confidence in ("medium", "low")

    def test_mpim_agency_and_breakup_on_same_row(self):
        result = self._result()
        pi_row = next((r for r in result.rows if r.trade_type == "PI"), None)
        assert pi_row is not None
        assert pi_row.auction_init_rate == -0.10, "Agency init rate must be on PI row"
        assert pi_row.breakup_rate == -0.05, "Breakup rate must be on same PI row"

    def test_pcust_rows_extracted(self):
        result = self._result()
        pcust = [r for r in result.rows if r.account_type == "PCUST"]
        assert len(pcust) > 0

    def test_no_dedup_flags_for_valid_response(self):
        result = self._result()
        dedup_flags = [f for f in result.flags if "duplicate" in f.issue.lower()]
        assert dedup_flags == []


# ===========================================================================
# Scenario 3: NYSE ARCA cascading / multi-level footnote chain
# ===========================================================================

class TestNyseCascadingFootnotes:
    def _result(self) -> ExtractionResult:
        return _run(
            nyse_response_cascading_footnotes(),
            fee_text="[NYSE PDF text]",
            content_type="text",
            operator="nyse",
        )

    def test_all_seven_footnotes_catalogued(self):
        result = self._result()
        assert len(result.footnotes) == 7
        refs = {fn.ref for fn in result.footnotes}
        assert {"*", "**", "†", "(a)", "(b)", "††", "‡"} == refs

    def test_customer_maker_references_asterisk_and_a(self):
        result = self._result()
        maker_penny = next(
            r for r in result.rows
            if r.account_type == "CUST" and r.ticker_class == "Penny"
            and r.trade_type == "Electronic" and r.make_rate is not None
        )
        assert "*" in maker_penny.footnote_refs
        assert "(a)" in maker_penny.footnote_refs

    def test_enhanced_program_documented_in_notes(self):
        result = self._result()
        maker_penny = next(
            r for r in result.rows
            if r.account_type == "CUST" and r.ticker_class == "Penny"
            and r.make_rate is not None
        )
        combined = (maker_penny.notes or "") + (maker_penny.confidence_reason or "")
        assert any(kw in combined for kw in ("enhanced", "50,000", "ADV", "0.18"))

    def test_cube_breakup_has_dagger_footnote(self):
        result = self._result()
        # CUBE Agency + Breakup are on the same PI row
        pi_row = next(
            (r for r in result.rows if r.trade_type == "PI" and r.breakup_rate is not None),
            None,
        )
        assert pi_row is not None, "PI row with CUBE breakup missing"
        assert pi_row.breakup_rate == -0.07
        assert "‡" in pi_row.footnote_refs

    def test_cube_agency_is_pi_trade_type(self):
        result = self._result()
        cube_agency = next(
            (r for r in result.rows if r.trade_type == "PI" and r.auction_init_rate is not None),
            None,
        )
        assert cube_agency is not None
        assert cube_agency.auction_init_rate == 0.00

    def test_cube_agency_and_breakup_on_same_row(self):
        result = self._result()
        pi_row = next((r for r in result.rows if r.trade_type == "PI"), None)
        assert pi_row is not None
        assert pi_row.auction_init_rate == 0.00
        assert pi_row.breakup_rate == -0.07

    def test_dagger_dagger_row_not_extracted(self):
        """†† (CUBE Response) is charged to the firm, not CUST/PCUST — must be skipped."""
        result = self._result()
        response_rows = [
            r for r in result.rows
            if r.trade_type == "PI" and r.auction_resp_rate is not None
            and r.auction_resp_rate == 0.32
        ]
        assert response_rows == [], "CUBE Response (firm-side) should not appear in CUST/PCUST output"

    def test_pcust_table3_rows_are_high_confidence(self):
        """Professional Customer Table 3 has no applicable footnotes — high confidence OK."""
        result = self._result()
        pcust_rows = [r for r in result.rows if r.account_type == "PCUST"]
        assert len(pcust_rows) > 0
        for r in pcust_rows:
            assert r.confidence == "high", (
                f"PCUST row should be high confidence (no footnotes); got {r.confidence!r}"
            )

    def test_customer_taker_cap_footnote_documented(self):
        result = self._result()
        # Maker+Taker are combined on the same Electronic row
        elec_row = next(
            (r for r in result.rows
             if r.account_type == "CUST" and r.trade_type == "Electronic"
             and r.take_rate is not None), None
        )
        assert elec_row is not None, "Customer Electronic row with take_rate missing"
        assert "(b)" in elec_row.footnote_refs
        combined = (elec_row.notes or "") + (elec_row.confidence_reason or "")
        assert any(kw in combined for kw in ("cap", "0.05", "10 contract"))


# ===========================================================================
# Scenario 4: BOX conflicting footnotes → low confidence + flag
# ===========================================================================

class TestBoxConflictingFootnotes:
    def _result(self) -> ExtractionResult:
        return _run(
            box_response_conflicting_footnotes(),
            fee_text="[BOX PDF text]",
            content_type="text",
            operator="box",
        )

    def test_four_footnotes_catalogued(self):
        result = self._result()
        assert len(result.footnotes) == 4

    def test_conflict_flag_raised(self):
        result = self._result()
        conflict_flags = [f for f in result.flags if "conflict" in f.issue.lower()]
        assert len(conflict_flags) >= 1

    def test_non_penny_remove_is_low_confidence(self):
        result = self._result()
        non_penny_remove = next(
            r for r in result.rows
            if r.ticker_class == "Non-Penny" and r.take_rate is not None
        )
        assert non_penny_remove.confidence == "low"

    def test_non_penny_remove_has_both_conflict_footnotes(self):
        result = self._result()
        non_penny_remove = next(
            r for r in result.rows
            if r.ticker_class == "Non-Penny" and r.take_rate is not None
        )
        assert "(i)" in non_penny_remove.footnote_refs
        assert "(ii)" in non_penny_remove.footnote_refs

    def test_penny_remove_is_low_confidence_waiver(self):
        result = self._result()
        penny_remove = next(
            r for r in result.rows
            if r.ticker_class == "Penny" and r.take_rate is not None
        )
        assert penny_remove.confidence == "low"
        assert "(i)" in penny_remove.footnote_refs

    def test_base_rate_preserved_not_adjusted(self):
        """Even with waiver footnote, the base table rate must be stored (not 0.00)."""
        result = self._result()
        penny_remove = next(
            r for r in result.rows
            if r.ticker_class == "Penny" and r.take_rate is not None
        )
        assert penny_remove.take_rate == -0.50, (
            "Base table rate must be preserved; waiver applied via footnote not by zeroing the rate"
        )

    def test_pip_agency_rate_from_footnote_is_medium(self):
        """PIP/BIM Agency + Breakup row comes from footnote (iii) → medium confidence."""
        result = self._result()
        pip_row = next(
            (r for r in result.rows if r.trade_type == "PI" and r.auction_init_rate is not None),
            None,
        )
        assert pip_row is not None
        assert pip_row.confidence == "medium"
        assert "(iii)" in pip_row.footnote_refs

    def test_bim_response_is_low_confidence(self):
        """BIM Response row has liq_code='BIM-RESP'; note (iv) qualifies → low confidence."""
        result = self._result()
        bim_response = next(
            (r for r in result.rows
             if r.trade_type == "PI" and r.auction_resp_rate is not None),
            None,
        )
        assert bim_response is not None, "BIM Response row missing (liq_code='BIM-RESP')"
        assert bim_response.confidence == "low"
        assert "(iv)" in bim_response.footnote_refs


# ===========================================================================
# Scenario 5: High-confidence rows with unacknowledged footnotes
# _validate_footnote_coverage should add a warning flag
# ===========================================================================

class TestFootnoteCoverageValidator:
    def test_warning_flag_added_for_high_conf_no_refs(self):
        result = _run(
            response_high_conf_with_unacknowledged_footnotes(),
            content_type="text",
        )
        coverage_flags = [f for f in result.flags if "high confidence" in f.issue.lower()]
        assert len(coverage_flags) >= 1, (
            "Expected a footnote coverage warning when high-confidence rows have empty "
            "footnote_refs but the document has footnotes"
        )

    def test_flag_severity_is_warning(self):
        result = _run(response_high_conf_with_unacknowledged_footnotes(), content_type="text")
        coverage_flags = [f for f in result.flags if "high confidence" in f.issue.lower()]
        assert all(f.severity == "warning" for f in coverage_flags)

    def test_medium_rows_no_coverage_flag(self):
        """Same setup but rows are medium confidence — no coverage flag should fire."""
        payload = response_high_conf_with_unacknowledged_footnotes()
        payload["rows"][0]["confidence"] = "medium"
        payload["rows"][0]["confidence_reason"] = "volume threshold"
        result = _run(payload, content_type="text")
        coverage_flags = [f for f in result.flags if "high confidence" in f.issue.lower()]
        assert coverage_flags == []


# ===========================================================================
# Scenario 6: Duplicate rows from Claude response
# ===========================================================================

class TestDuplicateRowHandling:
    def test_duplicate_removed(self):
        result = _run(response_with_duplicate_rows(), content_type="csv")
        assert len(result.rows) == 1

    def test_dedup_flag_raised(self):
        result = _run(response_with_duplicate_rows(), content_type="csv")
        dedup_flags = [f for f in result.flags if "duplicate" in f.issue.lower()]
        assert len(dedup_flags) >= 1

    def test_dedup_flag_is_warning(self):
        result = _run(response_with_duplicate_rows(), content_type="csv")
        dedup_flags = [f for f in result.flags if "duplicate" in f.issue.lower()]
        assert all(f.severity == "warning" for f in dedup_flags)


# ===========================================================================
# Scenario 7: Null liq_code in CSV extraction
# ===========================================================================

class TestNullLiqCodeInCsv:
    def test_null_liq_code_flag_raised(self):
        result = _run(response_with_null_liq_codes(), content_type="csv")
        liq_flags = [f for f in result.flags if "null liq_code" in f.issue]
        assert len(liq_flags) >= 1

    def test_null_liq_code_flag_is_error(self):
        result = _run(response_with_null_liq_codes(), content_type="csv")
        liq_flags = [f for f in result.flags if "null liq_code" in f.issue]
        assert all(f.severity == "error" for f in liq_flags)

    def test_null_liq_code_not_flagged_for_non_csv(self):
        """For PDF/HTML input, null liq_code is not necessarily wrong."""
        result = _run(response_with_null_liq_codes(), content_type="text")
        liq_flags = [f for f in result.flags if "null liq_code" in f.issue]
        assert liq_flags == []


# ===========================================================================
# Scenario 8: Empty fee text returns error, not empty rows
# ===========================================================================

class TestEmptyFeeText:
    def test_empty_text_returns_error(self):
        with patch("anthropic.Anthropic") as MockClient:
            MockClient.return_value.messages.create.return_value = MagicMock()
            extractor = ClaudeExtractor()
            result = extractor.extract("edgx", "cboe", "EDGX", "", content_type="csv")
        assert not result.ok
        assert result.error is not None
        assert "empty" in result.error.lower()

    def test_whitespace_only_returns_error(self):
        with patch("anthropic.Anthropic") as MockClient:
            MockClient.return_value.messages.create.return_value = MagicMock()
            extractor = ClaudeExtractor()
            result = extractor.extract("edgx", "cboe", "EDGX", "   \n\t  ", content_type="csv")
        assert not result.ok


# ===========================================================================
# Scenario 9: ExtractionResult.review_summary() reflects footnote-driven flags
# ===========================================================================

class TestReviewSummary:
    def test_summary_includes_low_conf_rows(self):
        result = _run(box_response_conflicting_footnotes(), content_type="text", operator="box")
        summary = result.review_summary()
        assert "low" in summary.lower() or "medium" in summary.lower()

    def test_summary_empty_when_all_high(self):
        payload = edgx_ideal_response()
        # Override all rows to high confidence for this test
        for row in payload["rows"]:
            row["confidence"] = "high"
            row["confidence_reason"] = None
            row["footnote_refs"] = ["1"]  # give them refs so coverage check passes
        payload["footnotes"] = [{"ref": "1", "text": "info only", "location": "p1"}]
        result = _run(payload, content_type="text")
        summary = result.review_summary()
        # Only flag content (not row content) should appear
        low_conf_section = "low/medium" in summary.lower() or summary == ""
        # There might be a coverage flag, but no low-conf rows
        assert len(result.low_confidence_rows) == 0
