"""Tests for MockExtractor — the API-free pipeline test mode."""
from __future__ import annotations

import pytest

from src.extractor.mock import MockExtractor, _BASE_RATES, _TICKER_CLASSES, _TRADE_TYPES, _SEC_TYPES, _ACCOUNT_TYPES


OPERATORS = list(_BASE_RATES.keys())


class TestMockExtractorBasics:
    def test_result_is_ok(self):
        result = MockExtractor().extract("edgx", "cboe", "CBOE EDGX", "[mock]")
        assert result.ok
        assert result.error is None

    def test_produces_rows(self):
        result = MockExtractor().extract("edgx", "cboe", "CBOE EDGX", "[mock]")
        assert len(result.rows) > 0

    def test_row_count_equals_combinations(self):
        expected = len(_TICKER_CLASSES) * len(_ACCOUNT_TYPES) * len(_TRADE_TYPES) * len(_SEC_TYPES)
        result = MockExtractor().extract("edgx", "cboe", "CBOE EDGX", "[mock]")
        assert len(result.rows) == expected

    def test_no_footnotes(self):
        result = MockExtractor().extract("edgx", "cboe", "CBOE EDGX", "[mock]")
        assert result.footnotes == []

    def test_no_flags(self):
        result = MockExtractor().extract("edgx", "cboe", "CBOE EDGX", "[mock]")
        assert result.flags == []

    def test_exchange_id_set_on_rows(self):
        result = MockExtractor().extract("bzx", "cboe", "CBOE BZX", "[mock]")
        assert all(r.exchange_id == "bzx" for r in result.rows)

    def test_zero_tokens(self):
        result = MockExtractor().extract("edgx", "cboe", "CBOE EDGX", "[mock]")
        assert result.input_tokens == 0
        assert result.output_tokens == 0

    def test_raw_response_is_mock_marker(self):
        result = MockExtractor().extract("edgx", "cboe", "CBOE EDGX", "[mock]")
        assert result.raw_response == "[mock]"


class TestMockExtractorRates:
    def test_all_ticker_classes_present(self):
        result = MockExtractor().extract("edgx", "cboe", "CBOE EDGX", "[mock]")
        classes = {r.ticker_class for r in result.rows}
        assert classes == set(_TICKER_CLASSES)

    def test_all_trade_types_present(self):
        result = MockExtractor().extract("edgx", "cboe", "CBOE EDGX", "[mock]")
        types = {r.trade_type for r in result.rows}
        assert types == set(_TRADE_TYPES)

    def test_all_account_types_present(self):
        result = MockExtractor().extract("edgx", "cboe", "CBOE EDGX", "[mock]")
        accounts = {r.account_type for r in result.rows}
        assert accounts == set(_ACCOUNT_TYPES)

    def test_all_sec_types_present(self):
        result = MockExtractor().extract("edgx", "cboe", "CBOE EDGX", "[mock]")
        sec = {r.sec_type for r in result.rows}
        assert sec == set(_SEC_TYPES)

    def test_pi_rows_have_auction_rates(self):
        result = MockExtractor().extract("edgx", "cboe", "CBOE EDGX", "[mock]")
        pi_rows = [r for r in result.rows if r.trade_type == "PI"]
        assert all(r.auction_init_rate is not None for r in pi_rows)
        assert all(r.auction_resp_rate is not None for r in pi_rows)

    def test_pi_and_solicitation_have_breakup(self):
        result = MockExtractor().extract("edgx", "cboe", "CBOE EDGX", "[mock]")
        for r in result.rows:
            if r.trade_type in ("PI", "Solicitation"):
                assert r.breakup_rate is not None
            else:
                assert r.breakup_rate is None

    def test_electronic_rows_have_make_and_take(self):
        result = MockExtractor().extract("edgx", "cboe", "CBOE EDGX", "[mock]")
        elec = [r for r in result.rows if r.trade_type == "Electronic"]
        assert all(r.make_rate is not None for r in elec)
        assert all(r.take_rate is not None for r in elec)

    def test_non_penny_make_rate_higher_than_penny(self):
        result = MockExtractor().extract("edgx", "cboe", "CBOE EDGX", "[mock]")
        penny_make = next(r.make_rate for r in result.rows
                          if r.ticker_class == "Penny" and r.trade_type == "Electronic"
                          and r.account_type == "CUST")
        nonpenny_make = next(r.make_rate for r in result.rows
                             if r.ticker_class == "Non-Penny" and r.trade_type == "Electronic"
                             and r.account_type == "CUST")
        assert nonpenny_make > penny_make

    def test_pcust_make_rate_lower_than_cust(self):
        result = MockExtractor().extract("edgx", "cboe", "CBOE EDGX", "[mock]")
        cust_make = next(r.make_rate for r in result.rows
                         if r.ticker_class == "Penny" and r.trade_type == "Electronic"
                         and r.account_type == "CUST")
        pcust_make = next(r.make_rate for r in result.rows
                          if r.ticker_class == "Penny" and r.trade_type == "Electronic"
                          and r.account_type == "PCUST")
        assert pcust_make < cust_make

    def test_base_rates_differ_per_operator(self):
        edgx = MockExtractor().extract("edgx", "cboe", "CBOE EDGX", "[mock]")
        nom  = MockExtractor().extract("nom",  "nasdaq", "NOM", "[mock]")
        edgx_make = next(r.make_rate for r in edgx.rows
                         if r.ticker_class == "Penny" and r.trade_type == "Electronic"
                         and r.account_type == "CUST")
        nom_make  = next(r.make_rate for r in nom.rows
                         if r.ticker_class == "Penny" and r.trade_type == "Electronic"
                         and r.account_type == "CUST")
        assert edgx_make != nom_make


class TestMockExtractorJitter:
    def test_jitter_false_rows_stable(self):
        r1 = MockExtractor(jitter=False).extract("edgx", "cboe", "CBOE EDGX", "[mock]")
        r2 = MockExtractor(jitter=False).extract("edgx", "cboe", "CBOE EDGX", "[mock]")
        rates1 = [(r.ticker_class, r.trade_type, r.take_rate) for r in r1.rows]
        rates2 = [(r.ticker_class, r.trade_type, r.take_rate) for r in r2.rows]
        assert rates1 == rates2

    def test_jitter_true_produces_same_row_count(self):
        base    = MockExtractor(jitter=False).extract("edgx", "cboe", "CBOE EDGX", "[mock]")
        jittered = MockExtractor(jitter=True).extract("edgx", "cboe",  "CBOE EDGX", "[mock]")
        assert len(base.rows) == len(jittered.rows)

    def test_jitter_content_type_passthrough(self):
        result = MockExtractor().extract("edgx", "cboe", "CBOE EDGX", "[mock]", content_type="csv")
        assert result.ok
