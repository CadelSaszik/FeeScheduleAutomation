"""Tests that verify the quality, completeness, and correctness of the extraction prompts.

These tests enforce the prompt requirements that are critical to accurate footnote handling
and confidence scoring.  If a test here fails, it means a prompt was edited in a way that
weakens the extraction quality rules.
"""
from __future__ import annotations

import pytest

from src.extractor.prompts import (
    DEFAULT_SYSTEM_PROMPT,
    OUTPUT_SCHEMA,
    SYSTEM_PROMPTS,
    build_user_message,
    get_system_prompt,
)

ALL_OPERATORS = ["cboe", "nasdaq", "nyse", "miax", "box", "memx"]


# ===========================================================================
# Operator coverage
# ===========================================================================

class TestOperatorCoverage:
    def test_all_operators_have_prompt(self):
        for op in ALL_OPERATORS:
            prompt = get_system_prompt(op)
            assert prompt, f"Operator '{op}' has no system prompt"
            assert len(prompt) > 200, f"Prompt for '{op}' is suspiciously short"

    def test_unknown_operator_returns_default(self):
        prompt = get_system_prompt("unknown_exchange_xyz")
        assert prompt == DEFAULT_SYSTEM_PROMPT

    def test_all_prompts_contain_output_schema(self):
        """Every operator prompt must embed the shared OUTPUT_SCHEMA."""
        for op in ALL_OPERATORS:
            prompt = get_system_prompt(op)
            assert "footnotes" in prompt.lower(), f"'{op}' prompt missing footnotes section"
            assert "confidence" in prompt.lower(), f"'{op}' prompt missing confidence section"
            assert "rows" in prompt.lower(), f"'{op}' prompt missing rows section"


# ===========================================================================
# Footnote pass 1 — mandatory scan
# ===========================================================================

class TestFootnotePassMandatory:
    def test_output_schema_marks_pass1_mandatory(self):
        assert "MANDATORY" in OUTPUT_SCHEMA, (
            "Pass 1 footnote scan must be marked MANDATORY in OUTPUT_SCHEMA"
        )

    def test_output_schema_warns_about_footnote_importance(self):
        schema_lower = OUTPUT_SCHEMA.lower()
        assert "almost universally" in schema_lower or "almost every" in schema_lower, (
            "OUTPUT_SCHEMA must warn that nearly every fee schedule row has applicable footnotes"
        )

    def test_pass1_instruction_present(self):
        assert "Pass 1" in OUTPUT_SCHEMA
        assert "Pass 2" in OUTPUT_SCHEMA

    def test_pass2_requires_checking_footnotes_per_row(self):
        assert "explicitly check" in OUTPUT_SCHEMA or "check:" in OUTPUT_SCHEMA.lower()


# ===========================================================================
# Confidence rules — high confidence must be earned
# ===========================================================================

class TestConfidenceRules:
    def test_high_confidence_requires_all_conditions(self):
        assert "ALL of the following" in OUTPUT_SCHEMA, (
            "High confidence must require ALL conditions, not just one"
        )

    def test_high_confidence_requires_source_citation(self):
        schema = OUTPUT_SCHEMA
        # source_page and source_section must be mentioned as high-confidence requirements
        high_section_start = schema.find("**high**")
        high_section_end = schema.find("**medium**")
        high_block = schema[high_section_start:high_section_end]
        assert "source_page" in high_block or "source_section" in high_block, (
            "High confidence rules must require source_page/source_section to be populated"
        )

    def test_high_confidence_requires_footnote_check(self):
        high_section_start = OUTPUT_SCHEMA.find("**high**")
        medium_section_start = OUTPUT_SCHEMA.find("**medium**")
        high_block = OUTPUT_SCHEMA[high_section_start:medium_section_start]
        assert "footnote_refs" in high_block, (
            "High confidence rules must require footnote_refs to be populated if footnotes exist"
        )

    def test_default_to_medium_instruction_present(self):
        assert "Default to medium" in OUTPUT_SCHEMA, (
            "OUTPUT_SCHEMA must instruct Claude to default to medium when unsure"
        )

    def test_high_is_exception_rule_present(self):
        assert "exception" in OUTPUT_SCHEMA.lower(), (
            "OUTPUT_SCHEMA must state that high confidence is the exception, not the rule"
        )

    def test_empty_footnote_refs_requires_verification(self):
        """An empty footnote_refs must be actively confirmed, not just left empty."""
        assert "empty array is a strong claim" in OUTPUT_SCHEMA or \
               "genuinely unaffected" in OUTPUT_SCHEMA or \
               "verify" in OUTPUT_SCHEMA.lower()


# ===========================================================================
# CBOE-specific prompt requirements
# ===========================================================================

class TestCboePrompt:
    def setup_method(self):
        self.prompt = get_system_prompt("cboe")

    def test_liq_code_is_required(self):
        assert "REQUIRED" in self.prompt or "must NEVER be null" in self.prompt, (
            "CBOE prompt must mark liq_code as required and non-null"
        )

    def test_one_csv_line_emits_one_or_two_rows(self):
        # No-class codes must expand to two rows (Penny + Non-Penny);
        # codes with a class qualifier emit exactly one row.
        assert "one or two" in self.prompt.lower() or "two rows" in self.prompt.lower(), (
            "CBOE prompt must explain that no-class codes emit two rows (Penny + Non-Penny)"
        )

    def test_no_consolidation_of_codes(self):
        assert "NEVER consolidate" in self.prompt or "Do not consolidate" in self.prompt, (
            "CBOE prompt must explicitly forbid consolidating codes"
        )

    def test_use_only_csv_values(self):
        assert "ONLY values that appear explicitly" in self.prompt or \
               "Use ONLY values" in self.prompt or \
               "use only CSV values" in self.prompt.lower(), (
            "CBOE prompt must forbid using prior knowledge to fill in rates"
        )

    def test_cboe_prompts_footnote_extraction_from_supplemental(self):
        # CBOE uses CSV + supplemental HTML. Prompt must instruct Claude to extract
        # footnotes from the supplemental content and link them to CSV rows.
        assert "supplemental" in self.prompt.lower() or \
               "two-pass" in self.prompt.lower() or \
               "Pass 1" in self.prompt, (
            "CBOE prompt must instruct Claude to extract footnotes from supplemental HTML content"
        )

    def test_confidence_reason_required_for_cboe(self):
        assert "confidence_reason" in self.prompt

    def test_footnote_limitation_acknowledged(self):
        # Prompt must acknowledge that the CSV lacks footnotes and explain how to handle it.
        assert "CSV" in self.prompt and (
            "footnote" in self.prompt.lower() or "supplemental" in self.prompt.lower()
        ), "CBOE prompt must explain the CSV/footnote relationship"

    def test_aim_agency_maps_to_auction_init(self):
        assert "auction_init_rate" in self.prompt

    def test_aim_response_maps_to_auction_resp(self):
        assert "auction_resp_rate" in self.prompt

    def test_qcc_maps_to_solicitation(self):
        assert "QCC" in self.prompt
        assert "Solicitation" in self.prompt

    def test_sam_maps_to_solicitation(self):
        assert "SAM" in self.prompt
        assert "Solicitation" in self.prompt

    def test_market_maker_skipped(self):
        assert "Market Maker" in self.prompt
        assert "skip" in self.prompt.lower() or "Skip" in self.prompt

    def test_breakup_rate_only_for_explicit_code(self):
        assert "only when" in self.prompt.lower() or "explicitly" in self.prompt.lower()


# ===========================================================================
# Nasdaq-specific prompt requirements
# ===========================================================================

class TestNasdaqPrompt:
    def setup_method(self):
        self.prompt = get_system_prompt("nasdaq")

    def test_empty_page_warning(self):
        # Prompt must tell Claude what to do if it receives a page with no fee tables
        assert "error flag" in self.prompt.lower() or "JS rendering" in self.prompt or \
               "no fee tables" in self.prompt.lower() or "zero rows" in self.prompt.lower()

    def test_footnotes_must_be_catalogued(self):
        assert "footnote" in self.prompt.lower()
        assert "catalog" in self.prompt.lower() or "catalogue" in self.prompt.lower() or \
               "MUST be catalogued" in self.prompt

    def test_superscript_footnotes_mentioned(self):
        assert "superscript" in self.prompt.lower() or "asterisk" in self.prompt.lower()

    def test_pi_auction_types_listed(self):
        assert "PIXL" in self.prompt
        assert "PRISM" in self.prompt

    def test_penny_pilot_mapping(self):
        assert "Penny Pilot" in self.prompt


# ===========================================================================
# NYSE-specific prompt requirements
# ===========================================================================

class TestNysePrompt:
    def setup_method(self):
        self.prompt = get_system_prompt("nyse")

    def test_cube_auction_mapped_to_pi(self):
        assert "CUBE" in self.prompt
        assert "PI" in self.prompt

    def test_subject_to_footnotes_warned(self):
        assert "subject to" in self.prompt.lower()

    def test_footnote_markers_mentioned(self):
        assert "asterisk" in self.prompt.lower() or "superscript" in self.prompt.lower()

    def test_pcust_requires_explicit_label(self):
        assert "explicitly labeled" in self.prompt or "explicitly" in self.prompt


# ===========================================================================
# MIAX-specific prompt requirements
# ===========================================================================

class TestMiaxPrompt:
    def setup_method(self):
        self.prompt = get_system_prompt("miax")

    def test_mpim_mapped_to_pi(self):
        assert "M-PIM" in self.prompt
        assert "PI" in self.prompt

    def test_rebate_sign_adjustment_mentioned(self):
        assert "negative values" in self.prompt.lower() or "adjust sign" in self.prompt.lower()

    def test_footnote_volume_tiers_warned(self):
        assert "volume tier" in self.prompt.lower() or "volume threshold" in self.prompt.lower()

    def test_priority_customer_mapping(self):
        assert "Priority Customer" in self.prompt
        assert "CUST" in self.prompt


# ===========================================================================
# BOX-specific prompt requirements
# ===========================================================================

class TestBoxPrompt:
    def setup_method(self):
        self.prompt = get_system_prompt("box")

    def test_pip_and_bim_mapped_to_pi(self):
        assert "PIP" in self.prompt
        assert "BIM" in self.prompt
        assert "PI" in self.prompt

    def test_pfof_footnotes_mentioned(self):
        assert "payment-for-order-flow" in self.prompt.lower() or "PFOF" in self.prompt

    def test_maker_taker_labels_mentioned(self):
        assert "Maker" in self.prompt
        assert "Taker" in self.prompt


# ===========================================================================
# User message builder
# ===========================================================================

class TestBuildUserMessage:
    def test_csv_message_mentions_csv_format(self):
        msg = build_user_message("CBOE EDGX", "CA,foo,-0.01\n", content_type="csv")
        assert "CSV" in msg or "csv" in msg.lower()
        assert "three columns" in msg.lower() or "3-column" in msg.lower() or \
               "Code, Description, Fee" in msg

    def test_csv_message_without_supplemental_guides_confidence(self):
        # Without supplemental HTML, the message should tell Claude how to apply confidence
        msg = build_user_message("CBOE EDGX", "CA,foo,-0.01\n", content_type="csv")
        assert "footnote" in msg.lower() or "confidence" in msg.lower()
        # Must NOT tell Claude to skip footnotes or return empty footnotes array
        assert "empty footnotes" not in msg.lower() and "no footnotes exist" not in msg.lower()

    def test_csv_message_with_supplemental_includes_instructions(self):
        msg = build_user_message("CBOE EDGX", "CA,foo,-0.01\n", content_type="csv",
                                 supplemental_text="Footnote 1: volume discount")
        assert "SUPPLEMENTAL" in msg or "supplemental" in msg.lower()
        assert "Pass 1" in msg or "footnote catalog" in msg.lower()

    def test_text_message_instructs_footnote_catalog(self):
        msg = build_user_message("MIAX", "some fee schedule", content_type="text")
        assert "footnote" in msg.lower()
        assert "catalog" in msg.lower() or "ALL footnotes" in msg or "footnotes first" in msg.lower()

    def test_long_text_truncated(self):
        long_text = "x" * 200_000
        msg = build_user_message("TEST", long_text, content_type="text")
        assert len(msg) < 160_000  # well under the 140k char limit + headers

    def test_exchange_name_in_message(self):
        msg = build_user_message("CBOE EDGX Options", "data", content_type="csv")
        assert "CBOE EDGX Options" in msg

    def test_truncation_marker_added(self):
        long_text = "y" * 200_000
        msg = build_user_message("TEST", long_text, content_type="text")
        assert "TRUNCATED" in msg


# ===========================================================================
# Rate extraction rules in OUTPUT_SCHEMA
# ===========================================================================

class TestRateExtractionRules:
    def test_rebates_positive_rule(self):
        assert "Rebates are POSITIVE" in OUTPUT_SCHEMA or "positive=rebate" in OUTPUT_SCHEMA.lower()

    def test_fees_negative_rule(self):
        assert "NEGATIVE" in OUTPUT_SCHEMA or "negative=fee" in OUTPUT_SCHEMA.lower()

    def test_two_decimal_places_rule(self):
        assert "2 decimal" in OUTPUT_SCHEMA or "two decimal" in OUTPUT_SCHEMA.lower()

    def test_null_not_zero_rule(self):
        assert "null" in OUTPUT_SCHEMA and "never use 0" in OUTPUT_SCHEMA.lower()

    def test_one_row_per_source_entry(self):
        assert "one row per source" in OUTPUT_SCHEMA.lower() or \
               "one row per liq_code" in OUTPUT_SCHEMA.lower()

    def test_only_csv_values_rule(self):
        assert "ONLY values that appear explicitly" in OUTPUT_SCHEMA or \
               "Use ONLY values" in OUTPUT_SCHEMA

    def test_exactly_one_rate_field_per_row(self):
        assert "EXACTLY ONE" in OUTPUT_SCHEMA or "exactly one" in OUTPUT_SCHEMA.lower()

    def test_cust_pcust_only_rule(self):
        assert "CUST" in OUTPUT_SCHEMA
        assert "PCUST" in OUTPUT_SCHEMA
        assert "Market Maker" in OUTPUT_SCHEMA
        assert "skip" in OUTPUT_SCHEMA.lower() or "Skip" in OUTPUT_SCHEMA

    def test_tier1_rule(self):
        assert "Tier 1" in OUTPUT_SCHEMA

    def test_base_table_rate_preserved(self):
        assert "BASE table rate" in OUTPUT_SCHEMA or "base table rate" in OUTPUT_SCHEMA.lower()
