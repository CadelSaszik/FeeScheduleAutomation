"""Comprehensive tests for the diff engine.

Covers: no changes, added rows, removed rows, modified rates (single and multi-field),
null↔value transitions, liq_code=null key handling, key normalisation, delta calculation,
degenerate inputs, and human-readable summary format.
"""
from __future__ import annotations

import pytest

from src.diff.engine import DiffEngine, DiffReport, RateChange, _norm, _row_key


def _row(
    exchange_id: str = "edgx",
    ticker_class: str = "Penny",
    sec_type: str = "OPT",
    account_type: str = "CUST",
    trade_type: str = "Electronic",
    liq_code: str | None = "CA",
    make_rate: float | None = None,
    take_rate: float | None = -0.47,
    auction_init_rate: float | None = None,
    auction_resp_rate: float | None = None,
    breakup_rate: float | None = None,
) -> dict:
    return {
        "exchange_id": exchange_id,
        "ticker_class": ticker_class,
        "sec_type": sec_type,
        "account_type": account_type,
        "trade_type": trade_type,
        "liq_code": liq_code,
        "make_rate": make_rate,
        "take_rate": take_rate,
        "auction_init_rate": auction_init_rate,
        "auction_resp_rate": auction_resp_rate,
        "breakup_rate": breakup_rate,
    }


ENGINE = DiffEngine()


# ===========================================================================
# No changes
# ===========================================================================

class TestNoChanges:
    def test_identical_rows_no_changes(self):
        rows = [_row(liq_code="CA"), _row(liq_code="NC")]
        report = ENGINE.diff("edgx", rows, rows)
        assert not report.has_changes

    def test_empty_old_and_new(self):
        report = ENGINE.diff("edgx", [], [])
        assert not report.has_changes

    def test_same_null_rates_not_changed(self):
        r = _row(take_rate=None, make_rate=None)
        report = ENGINE.diff("edgx", [r], [r])
        assert not report.has_changes

    def test_floating_point_noise_ignored(self):
        """0.10 stored as 0.1000000001 must not register as a change."""
        old = [_row(take_rate=-0.47)]
        new = [_row(take_rate=-0.4700000001)]
        report = ENGINE.diff("edgx", old, new)
        assert not report.has_changes


# ===========================================================================
# Added rows
# ===========================================================================

class TestAddedRows:
    def test_single_row_added(self):
        old = [_row(liq_code="CA")]
        new = [_row(liq_code="CA"), _row(liq_code="NC")]
        report = ENGINE.diff("edgx", old, new)
        assert report.has_changes
        assert len(report.added) == 1
        assert report.added[0].key["liq_code"] == "nc"

    def test_all_rows_added_when_old_empty(self):
        new = [_row(liq_code="CA"), _row(liq_code="NC"), _row(liq_code="ZA")]
        report = ENGINE.diff("edgx", [], new)
        assert len(report.added) == 3
        assert len(report.removed) == 0
        assert len(report.modified) == 0

    def test_added_row_carries_new_row_data(self):
        new = [_row(liq_code="CA", take_rate=-0.47)]
        report = ENGINE.diff("edgx", [], new)
        assert report.added[0].new_row is not None
        assert report.added[0].new_row["take_rate"] == -0.47


# ===========================================================================
# Removed rows
# ===========================================================================

class TestRemovedRows:
    def test_single_row_removed(self):
        old = [_row(liq_code="CA"), _row(liq_code="NC")]
        new = [_row(liq_code="CA")]
        report = ENGINE.diff("edgx", old, new)
        assert len(report.removed) == 1
        assert report.removed[0].key["liq_code"] == "nc"

    def test_all_rows_removed_when_new_empty(self):
        old = [_row(liq_code="CA"), _row(liq_code="NC")]
        report = ENGINE.diff("edgx", old, [])
        assert len(report.removed) == 2
        assert len(report.added) == 0

    def test_removed_row_carries_old_row_data(self):
        old = [_row(liq_code="CA", take_rate=-0.47)]
        report = ENGINE.diff("edgx", old, [])
        assert report.removed[0].old_row["take_rate"] == -0.47


# ===========================================================================
# Modified rows
# ===========================================================================

class TestModifiedRows:
    def test_take_rate_change_detected(self):
        old = [_row(liq_code="CA", take_rate=-0.47)]
        new = [_row(liq_code="CA", take_rate=-0.50)]
        report = ENGINE.diff("edgx", old, new)
        assert len(report.modified) == 1
        rc = report.modified[0].rate_changes[0]
        assert rc.field == "take_rate"
        assert rc.old_value == -0.47
        assert rc.new_value == -0.50

    def test_make_rate_change_detected(self):
        old = [_row(liq_code="CA", make_rate=0.10, take_rate=None)]
        new = [_row(liq_code="CA", make_rate=0.12, take_rate=None)]
        report = ENGINE.diff("edgx", old, new)
        assert len(report.modified) == 1
        assert report.modified[0].rate_changes[0].field == "make_rate"

    def test_auction_init_rate_change_detected(self):
        old = [_row(liq_code="BC", auction_init_rate=-0.06)]
        new = [_row(liq_code="BC", auction_init_rate=-0.08)]
        report = ENGINE.diff("edgx", old, new)
        assert len(report.modified) == 1

    def test_breakup_rate_change_detected(self):
        old = [_row(liq_code="CA", breakup_rate=-0.05)]
        new = [_row(liq_code="CA", breakup_rate=-0.07)]
        report = ENGINE.diff("edgx", old, new)
        assert len(report.modified) == 1
        assert report.modified[0].rate_changes[0].field == "breakup_rate"

    def test_multiple_rate_fields_changed(self):
        old = [_row(liq_code="CA", take_rate=-0.47, make_rate=0.10)]
        new = [_row(liq_code="CA", take_rate=-0.50, make_rate=0.12)]
        report = ENGINE.diff("edgx", old, new)
        assert len(report.modified) == 1
        assert len(report.modified[0].rate_changes) == 2

    def test_null_to_value_is_modification(self):
        old = [_row(liq_code="CA", breakup_rate=None)]
        new = [_row(liq_code="CA", breakup_rate=-0.05)]
        report = ENGINE.diff("edgx", old, new)
        assert len(report.modified) == 1
        rc = report.modified[0].rate_changes[0]
        assert rc.old_value is None
        assert rc.new_value == -0.05

    def test_value_to_null_is_modification(self):
        old = [_row(liq_code="CA", breakup_rate=-0.05)]
        new = [_row(liq_code="CA", breakup_rate=None)]
        report = ENGINE.diff("edgx", old, new)
        assert len(report.modified) == 1
        rc = report.modified[0].rate_changes[0]
        assert rc.old_value == -0.05
        assert rc.new_value is None

    def test_non_rate_field_changes_not_detected(self):
        """Only rate fields trigger modification; metadata changes (confidence, notes) do not."""
        old = [_row(liq_code="CA", take_rate=-0.47)]
        new = [_row(liq_code="CA", take_rate=-0.47)]
        report = ENGINE.diff("edgx", old, new)
        assert not report.has_changes


# ===========================================================================
# Delta calculation
# ===========================================================================

class TestDeltaCalculation:
    def test_positive_delta_fee_decrease(self):
        """A take_rate going from -0.50 to -0.47 means the fee decreased by $0.03."""
        rc = RateChange("take_rate", "Take Rate", old_value=-0.50, new_value=-0.47)
        assert rc.delta == 0.03

    def test_negative_delta_fee_increase(self):
        rc = RateChange("take_rate", "Take Rate", old_value=-0.47, new_value=-0.50)
        assert rc.delta == -0.03

    def test_delta_none_when_old_none(self):
        rc = RateChange("breakup_rate", "Breakup Rate", old_value=None, new_value=-0.05)
        assert rc.delta is None

    def test_delta_none_when_new_none(self):
        rc = RateChange("make_rate", "Make Rate", old_value=0.10, new_value=None)
        assert rc.delta is None

    def test_fmt_with_delta(self):
        rc = RateChange("take_rate", "Take Rate", old_value=-0.47, new_value=-0.50)
        s = rc.fmt()
        assert "Take Rate" in s
        assert "-$0.47" in s
        assert "-$0.50" in s
        assert "-0.03" in s

    def test_fmt_without_delta_null_old(self):
        rc = RateChange("breakup_rate", "Breakup Rate", old_value=None, new_value=-0.05)
        s = rc.fmt()
        assert "—" in s  # null formatted as em-dash
        assert "-$0.05" in s


# ===========================================================================
# Key normalisation
# ===========================================================================

class TestKeyNormalisation:
    def test_key_is_case_insensitive(self):
        """'Penny' and 'penny' must produce the same key so rows match across runs."""
        r1 = _row(ticker_class="Penny")
        r2 = _row(ticker_class="penny")
        k1 = _row_key(r1)
        k2 = _row_key(r2)
        assert k1 == k2

    def test_null_liq_code_produces_stable_key(self):
        r1 = _row(liq_code=None)
        r2 = _row(liq_code=None)
        assert _row_key(r1) == _row_key(r2)

    def test_different_liq_codes_produce_different_keys(self):
        assert _row_key(_row(liq_code="CA")) != _row_key(_row(liq_code="NC"))

    def test_whitespace_stripped_from_key(self):
        r1 = _row(liq_code="CA ")
        r2 = _row(liq_code="CA")
        assert _row_key(r1) == _row_key(r2)


# ===========================================================================
# Mixed scenarios (adds + removes + modifies in one diff)
# ===========================================================================

class TestMixedScenario:
    def test_add_remove_modify_simultaneously(self):
        old = [
            _row(liq_code="CA", take_rate=-0.47),
            _row(liq_code="NC", take_rate=-0.47),  # will be removed
        ]
        new = [
            _row(liq_code="CA", take_rate=-0.50),  # modified
            _row(liq_code="ZA", take_rate=-0.39),  # added
        ]
        report = ENGINE.diff("edgx", old, new)
        assert len(report.added) == 1
        assert len(report.removed) == 1
        assert len(report.modified) == 1
        assert report.total_changes == 3

    def test_summary_lines_cover_all_change_types(self):
        old = [_row(liq_code="CA", take_rate=-0.47), _row(liq_code="NC")]
        new = [_row(liq_code="CA", take_rate=-0.50), _row(liq_code="ZA")]
        report = ENGINE.diff("edgx", old, new)
        lines = report.summary_lines()
        text = "\n".join(lines)
        assert "MOD" in text
        assert "ADD" in text
        assert "DEL" in text

    def test_no_changes_summary(self):
        rows = [_row(liq_code="CA")]
        report = ENGINE.diff("edgx", rows, rows)
        lines = report.summary_lines()
        assert "No changes" in lines[0]


# ===========================================================================
# DiffReport totals
# ===========================================================================

class TestDiffReportTotals:
    def test_total_changes_counts_all_types(self):
        old = [_row(liq_code="CA"), _row(liq_code="NC")]
        new = [_row(liq_code="CA", take_rate=-0.50), _row(liq_code="ZA")]
        report = ENGINE.diff("edgx", old, new)
        assert report.total_changes == len(report.added) + len(report.removed) + len(report.modified)

    def test_has_changes_false_for_empty(self):
        report = ENGINE.diff("edgx", [], [])
        assert not report.has_changes
        assert report.total_changes == 0


# ===========================================================================
# _norm helper
# ===========================================================================

class TestIndexRowsDuplicateKey:
    def test_duplicate_key_in_same_set_keeps_last(self):
        """Two rows with the same key in the *same* set — _index_rows keeps the last one.
        This hits the logger.debug branch at line 186."""
        r1 = _row(liq_code="CA", take_rate=-0.47)
        r2 = _row(liq_code="CA", take_rate=-0.50)  # same key, different rate

        # Diffing old=[r1, r2] against new=[] — both old rows have the same key.
        # _index_rows(old) will log the duplicate and keep the last value (-0.50).
        report = ENGINE.diff("edgx", [r1, r2], [])
        assert len(report.removed) == 1
        assert report.removed[0].old_row["take_rate"] == -0.50  # last wins


class TestFmtRatePositive:
    def test_positive_rate_formatted_with_plus(self):
        """_fmt_rate with a positive value hits the `return f'+${value:.2f}'` branch (line 221)."""
        from src.diff.engine import _fmt_rate
        assert _fmt_rate(0.12) == "+$0.12"

    def test_positive_rate_in_rate_change_fmt(self):
        rc = RateChange("make_rate", "Make Rate", old_value=0.10, new_value=0.12)
        s = rc.fmt()
        assert "+$0.12" in s
        assert "+$0.10" in s

    def test_zero_is_formatted_as_positive(self):
        from src.diff.engine import _fmt_rate
        assert _fmt_rate(0.00) == "+$0.00"


class TestNorm:
    def test_none_stays_none(self):
        assert _norm(None) is None

    def test_float_rounded(self):
        assert _norm(0.123456) == 0.12

    def test_negative(self):
        assert _norm(-0.47) == -0.47

    def test_string_int(self):
        assert _norm("5") == 5.0

    def test_invalid_returns_none(self):
        assert _norm("bad") is None
