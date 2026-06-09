"""Integration tests for EDGX extraction quality — real Anthropic API calls.

Each test class is one vertical slice:
  Slice 1 — all rows have a ticker_class (Penny or Non-Penny)
  Slice 2 — PCUST MLEG rows (ZF/ZG/ZH/ZJ) are extracted
  Slice 3 — AIM Contra (BB/BF) maps to breakup_rate (contra/initiating firm fee)

Run the full quality suite:
    pytest tests/test_edgx_extraction_quality.py -v -m api

Run a single slice:
    pytest tests/test_edgx_extraction_quality.py::TestSlice1AllRowsHaveClass -v -m api
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

EDGX_CSV_PATH = (
    Path(__file__).parent.parent / "data/raw/edgx/edgx_20260608T202724Z.csv"
)

# Codes whose descriptions have no Penny/Non-Penny qualifier — must expand to 2 rows
NO_CLASS_CODES = {"CA", "CC", "QA", "QC", "QO", "QP", "SB", "SC", "SG", "SH", "ZC", "ZD"}

# PCUST MLEG codes that must appear in output
PCUST_MLEG_CODES = {
    "ZF": ("Penny",     "make_rate"),
    "ZG": ("Penny",     "take_rate"),
    "ZH": ("Non-Penny", "make_rate"),
    "ZJ": ("Non-Penny", "take_rate"),
}


def _load_csv() -> str:
    if not EDGX_CSV_PATH.exists():
        pytest.skip(f"EDGX CSV not found at {EDGX_CSV_PATH}")
    return EDGX_CSV_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def edgx_result():
    """Run extraction once per module; reused by all three slice classes.

    Skips if ANTHROPIC_API_KEY is absent or clearly a placeholder (< 20 chars).
    """
    import os
    from dotenv import load_dotenv
    from src.extractor.claude import ClaudeExtractor

    load_dotenv()
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if len(key) < 20:
        pytest.skip("ANTHROPIC_API_KEY not set or is a placeholder")

    csv_text = _load_csv()
    extractor = ClaudeExtractor()
    result = extractor.extract(
        exchange_id="edgx",
        operator="cboe",
        exchange_name="CBOE EDGX Options",
        fee_text=csv_text,
        content_type="csv",
    )
    if not result.ok:
        pytest.skip(f"Extraction failed: {result.error}")
    return result


# ---------------------------------------------------------------------------
# Slice 1 — Every row must have a ticker_class
# ---------------------------------------------------------------------------

@pytest.mark.api
class TestSlice1AllRowsHaveClass:
    """No extracted EDGX row should have ticker_class=None.

    Codes with no Penny/Non-Penny qualifier in their description apply to
    both classes and must produce TWO rows (one Penny, one Non-Penny).
    """

    def test_no_null_ticker_class(self, edgx_result):
        null_rows = [r for r in edgx_result.rows if r.ticker_class is None]
        codes = sorted({r.liq_code for r in null_rows})
        assert not null_rows, (
            f"Found {len(null_rows)} rows with ticker_class=None. "
            f"Liq codes: {codes}"
        )

    @pytest.mark.parametrize("code", sorted(NO_CLASS_CODES))
    def test_no_class_code_produces_penny_row(self, edgx_result, code):
        matching = [r for r in edgx_result.rows if r.liq_code == code]
        penny = [r for r in matching if r.ticker_class == "Penny"]
        assert penny, (
            f"Code {code} has no Penny row. "
            f"Got ticker_classes: {[r.ticker_class for r in matching]}"
        )

    @pytest.mark.parametrize("code", sorted(NO_CLASS_CODES))
    def test_no_class_code_produces_non_penny_row(self, edgx_result, code):
        matching = [r for r in edgx_result.rows if r.liq_code == code]
        non_penny = [r for r in matching if r.ticker_class == "Non-Penny"]
        assert non_penny, (
            f"Code {code} has no Non-Penny row. "
            f"Got ticker_classes: {[r.ticker_class for r in matching]}"
        )


# ---------------------------------------------------------------------------
# Slice 2 — PCUST MLEG rows (ZF/ZG/ZH/ZJ) extracted
# ---------------------------------------------------------------------------

@pytest.mark.api
class TestSlice2PcustMlegRows:
    """Complex order Non-Customer codes must be extracted as PCUST MLEG."""

    @pytest.mark.parametrize("code,expected_class,rate_field", [
        ("ZF", "Penny",     "make_rate"),
        ("ZG", "Penny",     "take_rate"),
        ("ZH", "Non-Penny", "make_rate"),
        ("ZJ", "Non-Penny", "take_rate"),
    ])
    def test_pcust_mleg_code_extracted(self, edgx_result, code, expected_class, rate_field):
        rows = [r for r in edgx_result.rows if r.liq_code == code]
        assert rows, f"Code {code} not found in extraction output at all"
        r = rows[0]
        assert r.account_type == "PCUST", (
            f"{code}: expected PCUST, got {r.account_type}"
        )
        assert r.sec_type == "MLEG", (
            f"{code}: expected MLEG, got {r.sec_type}"
        )
        assert r.ticker_class == expected_class, (
            f"{code}: expected ticker_class={expected_class!r}, got {r.ticker_class!r}"
        )
        assert getattr(r, rate_field) is not None, (
            f"{code}: expected {rate_field} to be set, got None"
        )


# ---------------------------------------------------------------------------
# Slice 3 — AIM Contra (BB/BF) → breakup_rate
# The contra/initiating firm fee when an AIM auction completes or breaks up.
# ---------------------------------------------------------------------------

@pytest.mark.api
class TestSlice3AimContraRateField:
    """AIM Contra codes map to breakup_rate (contra/initiating firm fee), not auction_resp_rate."""

    @pytest.mark.parametrize("code,expected_class", [
        ("BB", "Penny"),
        ("BF", "Non-Penny"),
    ])
    def test_aim_contra_extracted(self, edgx_result, code, expected_class):
        rows = [r for r in edgx_result.rows if r.liq_code == code]
        assert rows, f"Code {code} not found in extraction output"

    @pytest.mark.parametrize("code,expected_class", [
        ("BB", "Penny"),
        ("BF", "Non-Penny"),
    ])
    def test_aim_contra_is_cust_pi(self, edgx_result, code, expected_class):
        rows = [r for r in edgx_result.rows if r.liq_code == code]
        assert rows, f"Code {code} not found"
        r = rows[0]
        assert r.account_type == "CUST", f"{code}: expected CUST, got {r.account_type}"
        assert r.trade_type == "PI",     f"{code}: expected PI, got {r.trade_type}"
        assert r.ticker_class == expected_class, (
            f"{code}: expected {expected_class!r}, got {r.ticker_class!r}"
        )

    @pytest.mark.parametrize("code", ["BB", "BF"])
    def test_aim_contra_uses_breakup_rate(self, edgx_result, code):
        rows = [r for r in edgx_result.rows if r.liq_code == code]
        assert rows, f"Code {code} not found"
        r = rows[0]
        assert r.breakup_rate is not None, (
            f"{code}: breakup_rate should be set (AIM Contra = contra/initiating firm fee)"
        )
        assert r.auction_resp_rate is None, (
            f"{code}: auction_resp_rate should be None (AIM Contra is not a third-party responder)"
        )
