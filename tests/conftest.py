"""Shared fixtures, mock response builders, and realistic fee schedule fixtures.

Design philosophy:
  - All fixtures are based on real CBOE/MIAX/NYSE/BOX fee schedule structures.
  - Footnote fixtures are intentionally adversarial: cascading references, conditional
    waivers, volume tiers, and conflicting modifiers — the same complexity you find in
    production documents.
  - No Anthropic API calls are made anywhere in the test suite. Claude responses are
    mocked via unittest.mock.patch.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.extractor.claude import ExtractionFlag, FeeRow, Footnote


# ---------------------------------------------------------------------------
# Datetime helper
# ---------------------------------------------------------------------------

NOW = datetime(2026, 6, 8, 17, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# FeeRow factory
# ---------------------------------------------------------------------------

def make_row(
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
    source_page: str = "CSV row: CA",
    source_section: str = "Customer (contra Non-Customer), adds liquidity",
    footnote_refs: list[str] | None = None,
    confidence: str = "medium",
    confidence_reason: str | None = "CBOE CSV export omits footnotes",
    notes: str | None = None,
    extracted_at: datetime | None = None,
) -> FeeRow:
    return FeeRow(
        exchange_id=exchange_id,
        extracted_at=extracted_at or NOW,
        ticker_class=ticker_class,
        sec_type=sec_type,
        account_type=account_type,
        trade_type=trade_type,
        liq_code=liq_code,
        make_rate=make_rate,
        take_rate=take_rate,
        auction_init_rate=auction_init_rate,
        auction_resp_rate=auction_resp_rate,
        breakup_rate=breakup_rate,
        source_page=source_page,
        source_section=source_section,
        footnote_refs=footnote_refs or [],
        confidence=confidence,
        confidence_reason=confidence_reason,
        notes=notes,
    )


def make_footnote(ref: str, text: str, location: str = "Page 1 bottom") -> Footnote:
    return Footnote(ref=ref, text=text, location=location)


# ---------------------------------------------------------------------------
# Mock Anthropic response builder
# ---------------------------------------------------------------------------

def mock_anthropic_response(response_json: dict) -> MagicMock:
    """Build a mock anthropic.Message that ClaudeExtractor._parse_response can consume."""
    msg = MagicMock()
    msg.content = [MagicMock()]
    msg.content[0].text = "```json\n" + json.dumps(response_json) + "\n```"
    msg.usage.input_tokens = 5000
    msg.usage.output_tokens = 1500
    return msg


# ---------------------------------------------------------------------------
# Realistic CBOE EDGX CSV text fixture
# (exactly as returned by the ?csv=true endpoint)
# ---------------------------------------------------------------------------

EDGX_CSV_TEXT = """\
BA,AIM Agency (Non-Customer),0.20
BB,"AIM Contra, Penny",0.05
BC,"AIM Agency (Customer), Penny",-0.06
BD,"AIM Response, Penny",0.50
BE,"AIM Response, Non-Penny",1.15
BF,"AIM Contra, Non-Penny",0.02
BG,"AIM Agency (Customer), Non-Penny",0.00
CA,"Customer (contra Non-Customer), adds liquidity",-0.01
CC,AIM Customer-to-Customer Immediate Cross,0.00
NB,"Broker Dealer, Non-Penny",0.75
NC,"Customer, Removes liquidity, Non-Penny",-0.01
NF,"Firm, Non-Penny",0.75
NM,"Adds liquidity (Market Maker), Non-Penny",0.20
NP,"Professional, Non-Penny",0.75
NT,"Removes liquidity (Market Maker), Non-Penny",0.30
OC,Complex Trades at the Open,0.00
OO,EDGX Options Opening,0
PB,"Broker Dealer, Penny",0.48
PC,"Customer, Removes liquidity, Penny",-0.01
PF,"Firm, Penny",0.45
PM,"Adds liquidity (Market Maker), Penny",0.20
PN,"Away Market Maker, Penny",0.48
PP,"Professional, Penny",0.48
PT,"Removes liquidity (Market Maker), Penny",0.24
QA,QCC Agency (Customer),0
QC,QCC Contra (Customer),0
QO,QCC Agency (Professional),0.00
QP,QCC Contra (Professional),0.00
SA,"SAM Agency (Non-Customer, Non-Professional)",0.20
SB,SAM Contra (Customer),0
SC,SAM Agency (Customer),0
SD,"SAM Response, Penny",.50
SE,"SAM Response, Non-Penny",1.05
SG,SAM Agency (Professional),0.04
SH,SAM Contra (Professional),0.04
TN,"Customer-to-Customer Trade, adds liquidity, Non-Penny",0.00
TP,"Customer-to-Customer Trade, adds liquidity, Penny",0.00
ZA,"Complex order, Customer (contra Non-Customer), Penny",-0.39
ZB,"Complex order, Customer (contra Non-Customer), Non-Penny",-0.75
ZC,"Complex order, Customer (contra Customer)",0
ZD,"Complex order legs into Simple Book, Customer",0
"""

# Expected CUST/PCUST codes from above CSV (used to verify completeness):
EDGX_CUST_CODES = {"CA", "NC", "PC", "BC", "BG", "QA", "QC", "SA", "SB", "SC",
                    "TN", "TP", "ZA", "ZB", "ZC", "ZD"}
EDGX_PCUST_CODES = {"NP", "PP", "QO", "QP", "SG", "SH"}


# ---------------------------------------------------------------------------
# Mock Claude response: ideal EDGX CSV extraction (one row per code, all medium)
# ---------------------------------------------------------------------------

def edgx_ideal_response() -> dict:
    """What a correct EDGX extraction should look like: one row per CUST/PCUST code,
    liq_code populated, all medium confidence, no invented rates."""
    return {
        "footnotes": [],
        "flags": [],
        "rows": [
            # Electronic customer adds
            {"ticker_class": None, "sec_type": "OPT", "account_type": "CUST",
             "trade_type": "Electronic", "liq_code": "CA",
             "make_rate": -0.01, "take_rate": None, "auction_init_rate": None,
             "auction_resp_rate": None, "breakup_rate": None,
             "source_page": "CSV row: CA",
             "source_section": "Customer (contra Non-Customer), adds liquidity",
             "footnote_refs": [], "confidence": "medium",
             "confidence_reason": "CBOE CSV export omits footnotes; full schedule may contain qualifying footnotes.",
             "notes": None},
            # Electronic customer removes Penny
            {"ticker_class": "Penny", "sec_type": "OPT", "account_type": "CUST",
             "trade_type": "Electronic", "liq_code": "PC",
             "make_rate": None, "take_rate": -0.01, "auction_init_rate": None,
             "auction_resp_rate": None, "breakup_rate": None,
             "source_page": "CSV row: PC",
             "source_section": "Customer, Removes liquidity, Penny",
             "footnote_refs": [], "confidence": "medium",
             "confidence_reason": "CBOE CSV export omits footnotes.",
             "notes": None},
            # Electronic customer removes Non-Penny
            {"ticker_class": "Non-Penny", "sec_type": "OPT", "account_type": "CUST",
             "trade_type": "Electronic", "liq_code": "NC",
             "make_rate": None, "take_rate": -0.01, "auction_init_rate": None,
             "auction_resp_rate": None, "breakup_rate": None,
             "source_page": "CSV row: NC",
             "source_section": "Customer, Removes liquidity, Non-Penny",
             "footnote_refs": [], "confidence": "medium",
             "confidence_reason": "CBOE CSV export omits footnotes.",
             "notes": None},
            # AIM Agency Customer Penny
            {"ticker_class": "Penny", "sec_type": "OPT", "account_type": "CUST",
             "trade_type": "PI", "liq_code": "BC",
             "make_rate": None, "take_rate": None, "auction_init_rate": -0.06,
             "auction_resp_rate": None, "breakup_rate": None,
             "source_page": "CSV row: BC",
             "source_section": "AIM Agency (Customer), Penny",
             "footnote_refs": [], "confidence": "medium",
             "confidence_reason": "CBOE CSV export omits footnotes.",
             "notes": None},
            # AIM Agency Customer Non-Penny
            {"ticker_class": "Non-Penny", "sec_type": "OPT", "account_type": "CUST",
             "trade_type": "PI", "liq_code": "BG",
             "make_rate": None, "take_rate": None, "auction_init_rate": 0.00,
             "auction_resp_rate": None, "breakup_rate": None,
             "source_page": "CSV row: BG",
             "source_section": "AIM Agency (Customer), Non-Penny",
             "footnote_refs": [], "confidence": "medium",
             "confidence_reason": "CBOE CSV export omits footnotes.",
             "notes": None},
            # QCC Agency Customer (Solicitation)
            {"ticker_class": None, "sec_type": "OPT", "account_type": "CUST",
             "trade_type": "Solicitation", "liq_code": "QA",
             "make_rate": None, "take_rate": None, "auction_init_rate": 0.00,
             "auction_resp_rate": None, "breakup_rate": None,
             "source_page": "CSV row: QA",
             "source_section": "QCC Agency (Customer)",
             "footnote_refs": [], "confidence": "medium",
             "confidence_reason": "CBOE CSV export omits footnotes.",
             "notes": None},
            # QCC Contra Customer (Solicitation)
            {"ticker_class": None, "sec_type": "OPT", "account_type": "CUST",
             "trade_type": "Solicitation", "liq_code": "QC",
             "make_rate": None, "take_rate": None, "auction_init_rate": None,
             "auction_resp_rate": 0.00, "breakup_rate": None,
             "source_page": "CSV row: QC",
             "source_section": "QCC Contra (Customer)",
             "footnote_refs": [], "confidence": "medium",
             "confidence_reason": "CBOE CSV export omits footnotes.",
             "notes": None},
            # SAM Agency Customer (Solicitation)
            {"ticker_class": None, "sec_type": "OPT", "account_type": "CUST",
             "trade_type": "Solicitation", "liq_code": "SC",
             "make_rate": None, "take_rate": None, "auction_init_rate": 0.00,
             "auction_resp_rate": None, "breakup_rate": None,
             "source_page": "CSV row: SC",
             "source_section": "SAM Agency (Customer)",
             "footnote_refs": [], "confidence": "medium",
             "confidence_reason": "CBOE CSV export omits footnotes.",
             "notes": None},
            # SAM Contra Customer (Solicitation)
            {"ticker_class": None, "sec_type": "OPT", "account_type": "CUST",
             "trade_type": "Solicitation", "liq_code": "SB",
             "make_rate": None, "take_rate": None, "auction_init_rate": None,
             "auction_resp_rate": 0.00, "breakup_rate": None,
             "source_page": "CSV row: SB",
             "source_section": "SAM Contra (Customer)",
             "footnote_refs": [], "confidence": "medium",
             "confidence_reason": "CBOE CSV export omits footnotes.",
             "notes": None},
            # Professional Penny (PCUST)
            {"ticker_class": "Penny", "sec_type": "OPT", "account_type": "PCUST",
             "trade_type": "Electronic", "liq_code": "PP",
             "make_rate": None, "take_rate": 0.48, "auction_init_rate": None,
             "auction_resp_rate": None, "breakup_rate": None,
             "source_page": "CSV row: PP",
             "source_section": "Professional, Penny",
             "footnote_refs": [], "confidence": "medium",
             "confidence_reason": "CBOE CSV export omits footnotes.",
             "notes": None},
            # Complex customer contra Non-Customer Penny (MLEG)
            {"ticker_class": "Penny", "sec_type": "MLEG", "account_type": "CUST",
             "trade_type": "Electronic", "liq_code": "ZA",
             "make_rate": None, "take_rate": -0.39, "auction_init_rate": None,
             "auction_resp_rate": None, "breakup_rate": None,
             "source_page": "CSV row: ZA",
             "source_section": "Complex order, Customer (contra Non-Customer), Penny",
             "footnote_refs": [], "confidence": "medium",
             "confidence_reason": "CBOE CSV export omits footnotes.",
             "notes": None},
        ],
    }


# ---------------------------------------------------------------------------
# Challenging footnote fixture: MIAX volume-conditional rebate
# Simulates a MIAX PDF where Priority Customer rebates are tiered by ADV
# ---------------------------------------------------------------------------

MIAX_PDF_TEXT_SNIPPET = """\
--- Page 2 ---
SECTION II: TRANSACTION FEES AND CREDITS

Table 2A: Priority Customer Transaction Fees

                        Penny Classes    Non-Penny Classes
Maker (Add Liquidity)    $0.28/contract¹   $0.35/contract¹
Taker (Remove Liq.)     -$0.48/contract²  -$0.55/contract²

¹ Priority Customer Maker rebate applies to members maintaining ≥ 1,000,000 ADV.
  Members below this threshold receive $0.20/contract (Penny) and $0.25/contract
  (Non-Penny) respectively. Tiered ADV is calculated monthly.
² Priority Customer Taker fee applies to all CUST orders. Subject to a per-trade
  fee cap of $0.03/contract when the contra-party is a MIAX Market Maker providing
  continuous quotes under Rule 803(a).

--- Page 3 ---
Table 2B: Priority Customer — M-PIM (Price Improvement Mechanism)

                        Penny    Non-Penny
Agency (Initiating)    -$0.10   -$0.05
Response               +$0.45   +$0.85
Breakup Fee            -$0.05*  -$0.05*

* Breakup fee applies when M-PIM auction does not execute (i.e. no response
  improves on the NBBO). Charged to the initiating order only.

--- Page 3 continued ---
Table 2C: Professional Customer Transaction Fees

                        Penny    Non-Penny
Maker (Add)            +$0.20   +$0.25
Taker (Remove)         -$0.50   -$0.58
"""


def miax_response_volume_conditional() -> dict:
    """Correct extraction of MIAX volume-conditional footnote scenario.

    Key checks:
    - Footnotes 1 and 2 are catalogued
    - Maker rebates use BASE rate ($0.28/$0.35) not the lower tier
    - Taker fee uses BASE rate with cap footnote recorded
    - AIM/M-PIM agency → auction_init_rate, response → auction_resp_rate
    - Breakup fee footnote (*) is recorded for breakup rows
    - All rows are medium or low confidence due to conditional footnotes
    """
    return {
        "footnotes": [
            {
                "ref": "1",
                "text": (
                    "Priority Customer Maker rebate applies to members maintaining ≥ 1,000,000 ADV. "
                    "Members below this threshold receive $0.20/contract (Penny) and $0.25/contract "
                    "(Non-Penny) respectively. Tiered ADV is calculated monthly."
                ),
                "location": "Page 2, below Table 2A",
            },
            {
                "ref": "2",
                "text": (
                    "Priority Customer Taker fee applies to all CUST orders. Subject to a per-trade "
                    "fee cap of $0.03/contract when the contra-party is a MIAX Market Maker providing "
                    "continuous quotes under Rule 803(a)."
                ),
                "location": "Page 2, below Table 2A",
            },
            {
                "ref": "*",
                "text": (
                    "Breakup fee applies when M-PIM auction does not execute (i.e. no response "
                    "improves on the NBBO). Charged to the initiating order only."
                ),
                "location": "Page 3, below Table 2B",
            },
        ],
        "flags": [],
        "rows": [
            # Penny — MIAX table has a single row with both Maker and Taker for the same class/type.
            # Both footnotes 1 (maker volume condition) and 2 (taker cap) apply to this row.
            {"ticker_class": "Penny", "sec_type": "OPT", "account_type": "CUST",
             "trade_type": "Electronic", "liq_code": None,
             "make_rate": 0.28, "take_rate": -0.48, "auction_init_rate": None,
             "auction_resp_rate": None, "breakup_rate": None,
             "source_page": "Page 2", "source_section": "Table 2A: Priority Customer Transaction Fees",
             "footnote_refs": ["1", "2"], "confidence": "medium",
             "confidence_reason": "Footnote 1: maker rebate volume conditional (≥1M ADV). Footnote 2: taker fee capped at $0.03 vs MIAX MM.",
             "notes": "Maker base $0.28 (below-ADV rate: $0.20). Taker base -$0.48 capped at $0.03 vs MIAX MM."},
            # Non-Penny — same structure
            {"ticker_class": "Non-Penny", "sec_type": "OPT", "account_type": "CUST",
             "trade_type": "Electronic", "liq_code": None,
             "make_rate": 0.35, "take_rate": -0.55, "auction_init_rate": None,
             "auction_resp_rate": None, "breakup_rate": None,
             "source_page": "Page 2", "source_section": "Table 2A: Priority Customer Transaction Fees",
             "footnote_refs": ["1", "2"], "confidence": "medium",
             "confidence_reason": "Footnote 1: maker volume threshold. Footnote 2: taker cap applies vs MIAX MM.",
             "notes": "Maker base $0.35 (below-threshold: $0.25). Taker base -$0.55 capped at $0.03 vs MIAX MM."},
            # M-PIM Penny — Agency (CUST init) + Breakup combined on one row.
            # M-PIM Response ($0.45) is charged to Market Maker responders, not CUST — omitted.
            # Footnote * qualifies the breakup_rate condition.
            {"ticker_class": "Penny", "sec_type": "OPT", "account_type": "CUST",
             "trade_type": "PI", "liq_code": None,
             "make_rate": None, "take_rate": None, "auction_init_rate": -0.10,
             "auction_resp_rate": None, "breakup_rate": -0.05,
             "source_page": "Page 3", "source_section": "Table 2B: Priority Customer — M-PIM",
             "footnote_refs": ["*"], "confidence": "medium",
             "confidence_reason": "Footnote *: breakup fee only when M-PIM does not execute (no response improves on NBBO).",
             "notes": "Charged to initiating order only. M-PIM Response ($0.45) charged to MM responders, not CUST."},
            # PCUST Maker Penny
            {"ticker_class": "Penny", "sec_type": "OPT", "account_type": "PCUST",
             "trade_type": "Electronic", "liq_code": None,
             "make_rate": 0.20, "take_rate": None, "auction_init_rate": None,
             "auction_resp_rate": None, "breakup_rate": None,
             "source_page": "Page 3", "source_section": "Table 2C: Professional Customer Transaction Fees",
             "footnote_refs": [], "confidence": "high",
             "confidence_reason": None,
             "notes": None},
        ],
    }


# ---------------------------------------------------------------------------
# Challenging footnote fixture: NYSE cascading / conflicting footnotes
# Simulates NYSE ARCA Options PDF with multi-level footnote chains
# ---------------------------------------------------------------------------

NYSE_PDF_TEXT_SNIPPET = """\
--- Page 1 ---
NYSE ARCA OPTIONS FEE SCHEDULE
Effective May 6, 2026

I. ELECTRONIC TRANSACTION FEES

Table 1: Customer Orders (all classes unless noted)

                        Penny Pilot   Non-Penny
Customer Add (Maker)    $0.12*        $0.15*
Customer Remove (Taker) -$0.45**      -$0.50**
Customer CUBE Agency†   $0.00         $0.00

* Applies to standard Customer orders. See footnote (a) for enhanced rebate program.
** Standard remove fee. See footnote (b) for cap conditions.
† CUBE = Customer Best Execution Auction (price-improvement mechanism).
  CUBE Agency rate is $0.00 for orders entering the auction as initiating customer.

(a) Enhanced Maker Rebate Program: Customers whose clearing firms submit ≥ 50,000
    Customer contracts/day (ADV measured over 21 trading days) qualify for enhanced
    rebate of $0.18 (Penny) / $0.22 (Non-Penny). Program enrollment required.
    Contact your account representative.

(b) Customer Taker fee subject to per-execution cap: total charge per execution shall
    not exceed $0.05/contract when the removing order is ≤ 10 contracts in size.
    Cap does not apply to orders > 10 contracts.

--- Page 2 ---
Table 2: CUBE Auction Fees (Price Improvement Mechanism)

                        All Classes
CUBE Agency (Customer)   $0.00
CUBE Response            $0.32††
CUBE Breakup Fee         -$0.07‡

†† CUBE Response fee charged to the firm submitting the response order, not the
   customer. Not applicable to CUST/PCUST rows.
‡  Breakup fee charged to Customer initiating CUBE auction when no price improvement
   is provided. Applies to all order sizes.

--- Page 2 continued ---
Table 3: Professional Customer Orders

                        Penny Pilot   Non-Penny
Professional Customer Maker    $0.05   $0.08
Professional Customer Taker   -$0.48  -$0.52
"""


def nyse_response_cascading_footnotes() -> dict:
    """Correct extraction for NYSE cascading footnote scenario.

    Key checks:
    - Footnotes *, **, †, (a), (b), ††, ‡ are ALL catalogued in Pass 1
    - Customer Maker rows reference footnotes * and (a) — base rate used, enhanced program noted
    - Customer Taker rows reference ** and (b) — base rate used, cap noted
    - CUBE Agency is $0.00 → auction_init_rate=0.00, trade_type=PI, confidence=high
      (no modifying footnotes)
    - CUBE Breakup ‡ → breakup_rate=-0.07, footnote ‡ referenced
    - CUBE Response †† → skipped (not CUST/PCUST)
    - PCUST rows from Table 3 — no footnotes apply, high confidence acceptable
    """
    return {
        "footnotes": [
            {"ref": "*",
             "text": "Applies to standard Customer orders. See footnote (a) for enhanced rebate program.",
             "location": "Page 1, Table 1 footnote"},
            {"ref": "**",
             "text": "Standard remove fee. See footnote (b) for cap conditions.",
             "location": "Page 1, Table 1 footnote"},
            {"ref": "†",
             "text": "CUBE = Customer Best Execution Auction. CUBE Agency rate is $0.00 for orders entering as initiating customer.",
             "location": "Page 1, Table 1 footnote"},
            {"ref": "(a)",
             "text": "Enhanced Maker Rebate Program: Customers whose clearing firms submit ≥ 50,000 Customer contracts/day ADV qualify for enhanced rebate of $0.18 (Penny) / $0.22 (Non-Penny). Program enrollment required.",
             "location": "Page 1, below Table 1"},
            {"ref": "(b)",
             "text": "Customer Taker fee subject to per-execution cap: total charge per execution shall not exceed $0.05/contract when the removing order is ≤ 10 contracts in size.",
             "location": "Page 1, below Table 1"},
            {"ref": "††",
             "text": "CUBE Response fee charged to the firm submitting the response order, not the customer. Not applicable to CUST/PCUST rows.",
             "location": "Page 2, Table 2 footnote"},
            {"ref": "‡",
             "text": "Breakup fee charged to Customer initiating CUBE auction when no price improvement is provided. Applies to all order sizes.",
             "location": "Page 2, Table 2 footnote"},
        ],
        "flags": [],
        "rows": [
            # Customer Penny Electronic — Maker and Taker on same row (both in Table 1).
            # Footnotes *, (a) for Maker; **, (b) for Taker — all four referenced.
            {"ticker_class": "Penny", "sec_type": "OPT", "account_type": "CUST",
             "trade_type": "Electronic", "liq_code": None,
             "make_rate": 0.12, "take_rate": -0.45, "auction_init_rate": None,
             "auction_resp_rate": None, "breakup_rate": None,
             "source_page": "Page 1", "source_section": "Table 1: Customer Orders",
             "footnote_refs": ["*", "(a)", "**", "(b)"], "confidence": "medium",
             "confidence_reason": "Footnote (a): enhanced maker rebate $0.18 for qualifying members. Footnote (b): taker cap $0.05 for ≤10 contract orders.",
             "notes": "Maker base $0.12; enhanced program ≥50k ADV → $0.18. Taker base -$0.45; capped at $0.05 for ≤10 contracts."},
            # Customer Non-Penny Electronic
            {"ticker_class": "Non-Penny", "sec_type": "OPT", "account_type": "CUST",
             "trade_type": "Electronic", "liq_code": None,
             "make_rate": 0.15, "take_rate": -0.50, "auction_init_rate": None,
             "auction_resp_rate": None, "breakup_rate": None,
             "source_page": "Page 1", "source_section": "Table 1: Customer Orders",
             "footnote_refs": ["*", "(a)", "**", "(b)"], "confidence": "medium",
             "confidence_reason": "Footnote (a): enhanced rebate $0.22. Footnote (b): taker cap applies.",
             "notes": "Maker base $0.15; enhanced → $0.22. Taker base -$0.50; cap applies ≤10 contracts."},
            # CUBE Agency + Breakup — both CUST PI attributes on one row.
            # † confirms agency is $0.00; ‡ qualifies breakup condition.
            # CUBE Response (†† = firm-side) is excluded.
            {"ticker_class": None, "sec_type": "OPT", "account_type": "CUST",
             "trade_type": "PI", "liq_code": None,
             "make_rate": None, "take_rate": None, "auction_init_rate": 0.00,
             "auction_resp_rate": None, "breakup_rate": -0.07,
             "source_page": "Page 2", "source_section": "Table 2: CUBE Auction Fees",
             "footnote_refs": ["†", "‡"], "confidence": "medium",
             "confidence_reason": "Footnote ‡: breakup only when CUBE provides no price improvement.",
             "notes": "CUBE Agency $0.00 (initiating customer). Breakup -$0.07 when no improvement provided."},
            # PCUST Penny Electronic — Table 3, no modifying footnotes
            {"ticker_class": "Penny", "sec_type": "OPT", "account_type": "PCUST",
             "trade_type": "Electronic", "liq_code": None,
             "make_rate": 0.05, "take_rate": -0.48, "auction_init_rate": None,
             "auction_resp_rate": None, "breakup_rate": None,
             "source_page": "Page 2", "source_section": "Table 3: Professional Customer Orders",
             "footnote_refs": [], "confidence": "high",
             "confidence_reason": None,
             "notes": None},
        ],
    }


# ---------------------------------------------------------------------------
# Adversarial fixture: conflicting footnotes that yield low confidence
# Simulates a BOX-style schedule where two footnotes give contradictory rates
# ---------------------------------------------------------------------------

BOX_PDF_TEXT_SNIPPET = """\
--- Page 1 ---
BOX EXCHANGE FEE SCHEDULE — Effective October 1, 2025

SECTION A: Public Customer Transaction Fees

                    Penny Classes    Non-Penny
Remove Liquidity   -$0.50/contract  -$0.65/contract
Add Liquidity       $0.10/contract   $0.15/contract

Note (i): Remove Liquidity fee is waived entirely for Public Customer orders
submitted by payment-for-order-flow participant firms under the BOX PFOF Program.

Note (ii): For orders submitted through Designated PFOF Participants in Non-Penny
classes, an additional rebate credit of $0.10/contract is applied, resulting in
an effective Remove rate of -$0.55/contract (not -$0.65/contract as stated above).

Note (iii): BOX PIP (Price Improvement Period) and BIM (BOX Improvement Mechanism)
are both price-improvement mechanisms and classified as PI trade type.
  - PIP Agency: $0.00 / BIM Agency: $0.00
  - PIP Response: +$0.40 / BIM Response: +$0.45 (see Note iv)
  - PIP Breakup: -$0.07 / BIM Breakup: -$0.08

Note (iv): BIM Response rate of $0.45 applies only when the BIM response order
provides improvement of at least $0.01/contract over the NBBO. If improvement
is exactly $0.01, rate is $0.35 instead.
"""


def box_response_conflicting_footnotes() -> dict:
    """Correct extraction for BOX conflicting-footnote scenario.

    Key checks:
    - Notes (i) and (ii) conflict: note (i) waives the -$0.50 fee entirely;
      note (ii) gives a different effective rate for Non-Penny PFOF orders.
      Both notes must be in footnote_refs; confidence = low for those rows.
    - Note (iii) provides PIP/BIM rates inline in the footnote itself — these
      should still be extracted as rows with note (iii) in footnote_refs.
    - Note (iv) qualifies the BIM Response rate — that row is low confidence.
    - PIP and BIM map to the same trade_type = "PI"; differentiated by notes.
    """
    return {
        "footnotes": [
            {"ref": "(i)",
             "text": "Remove Liquidity fee is waived entirely for Public Customer orders submitted by payment-for-order-flow participant firms under the BOX PFOF Program.",
             "location": "Page 1, below Table Section A"},
            {"ref": "(ii)",
             "text": "For orders submitted through Designated PFOF Participants in Non-Penny classes, an additional rebate credit of $0.10/contract is applied, resulting in an effective Remove rate of -$0.55/contract (not -$0.65/contract as stated above).",
             "location": "Page 1, below Table Section A"},
            {"ref": "(iii)",
             "text": "BOX PIP and BIM are both price-improvement mechanisms (PI trade type). PIP Agency: $0.00. BIM Agency: $0.00. PIP Response: +$0.40. BIM Response: +$0.45 (see Note iv). PIP Breakup: -$0.07. BIM Breakup: -$0.08.",
             "location": "Page 1, below Table Section A"},
            {"ref": "(iv)",
             "text": "BIM Response rate of $0.45 applies only when response provides improvement ≥ $0.01/contract over NBBO. If improvement is exactly $0.01, rate is $0.35 instead.",
             "location": "Page 1, below Table Section A"},
        ],
        "flags": [
            {"severity": "warning",
             "location": "Page 1, Section A, Non-Penny Remove",
             "issue": "Notes (i) and (ii) conflict: note (i) waives the fee entirely for PFOF participants; note (ii) gives a different effective rate for PFOF Non-Penny. Effective rate is indeterminate without knowing PFOF program status."},
        ],
        "rows": [
            # Remove Penny — note (i) potentially waives entire fee → low confidence
            {"ticker_class": "Penny", "sec_type": "OPT", "account_type": "CUST",
             "trade_type": "Electronic", "liq_code": None,
             "make_rate": None, "take_rate": -0.50, "auction_init_rate": None,
             "auction_resp_rate": None, "breakup_rate": None,
             "source_page": "Page 1", "source_section": "Section A: Public Customer Transaction Fees",
             "footnote_refs": ["(i)"], "confidence": "low",
             "confidence_reason": "Note (i): fee is waived entirely for PFOF program participants. Effective rate depends on PFOF status.",
             "notes": "Base fee -$0.50; waived for PFOF participants."},
            # Remove Non-Penny — notes (i) and (ii) conflict → low confidence
            {"ticker_class": "Non-Penny", "sec_type": "OPT", "account_type": "CUST",
             "trade_type": "Electronic", "liq_code": None,
             "make_rate": None, "take_rate": -0.65, "auction_init_rate": None,
             "auction_resp_rate": None, "breakup_rate": None,
             "source_page": "Page 1", "source_section": "Section A: Public Customer Transaction Fees",
             "footnote_refs": ["(i)", "(ii)"], "confidence": "low",
             "confidence_reason": "Notes (i) and (ii) conflict: (i) waives fee; (ii) gives -$0.55 effective rate for PFOF Non-Penny. Cannot determine true rate without PFOF status.",
             "notes": "Table shows -$0.65. Note (ii) PFOF effective rate: -$0.55. Note (i) full waiver for PFOF. Conflict flagged."},
            # Add Penny
            {"ticker_class": "Penny", "sec_type": "OPT", "account_type": "CUST",
             "trade_type": "Electronic", "liq_code": None,
             "make_rate": 0.10, "take_rate": None, "auction_init_rate": None,
             "auction_resp_rate": None, "breakup_rate": None,
             "source_page": "Page 1", "source_section": "Section A: Public Customer Transaction Fees",
             "footnote_refs": [], "confidence": "high",
             "confidence_reason": None, "notes": None},
            # PIP/BIM Customer PI — Agency + Breakup on one row (both CUST-side fees).
            # Rates are sourced from footnote (iii) (not the primary table), so medium confidence.
            # BIM Response ($0.45) is the contra-side fee charged to responders, not CUST — excluded.
            # Using PIP breakup (-$0.07); BIM breakup (-$0.08) noted in notes field.
            {"ticker_class": None, "sec_type": "OPT", "account_type": "CUST",
             "trade_type": "PI", "liq_code": None,
             "make_rate": None, "take_rate": None, "auction_init_rate": 0.00,
             "auction_resp_rate": None, "breakup_rate": -0.07,
             "source_page": "Page 1", "source_section": "Note (iii): PIP/BIM rates",
             "footnote_refs": ["(iii)"], "confidence": "medium",
             "confidence_reason": "PIP/BIM rates sourced from footnote (iii), not a primary table cell.",
             "notes": "PIP Agency $0.00, BIM Agency $0.00. PIP Breakup -$0.07; BIM Breakup -$0.08. BIM Response ($0.45) is contra-side, not CUST."},
            # BIM Response — Note (iv) qualifies: rate changes at minimum improvement.
            # This is a separate row for the CUST who receives a response (contra to their order).
            # In some fee structures, CUST benefits from contra response so it's relevant.
            # liq_code differentiator: use "BIM-RESP" to distinguish from PIP/BIM Agency row.
            {"ticker_class": None, "sec_type": "OPT", "account_type": "CUST",
             "trade_type": "PI", "liq_code": "BIM-RESP",
             "make_rate": None, "take_rate": None, "auction_init_rate": None,
             "auction_resp_rate": 0.45, "breakup_rate": None,
             "source_page": "Page 1", "source_section": "Note (iii): PIP/BIM rates",
             "footnote_refs": ["(iii)", "(iv)"], "confidence": "low",
             "confidence_reason": "Note (iv): BIM Response $0.45 only when improvement > $0.01/contract; exactly $0.01 improvement yields $0.35.",
             "notes": "BIM Response base $0.45; drops to $0.35 if improvement is exactly $0.01."},
        ],
    }


# ---------------------------------------------------------------------------
# Adversarial fixture: high-confidence rows in a document that has footnotes
# (should trigger _validate_footnote_coverage warning flag)
# ---------------------------------------------------------------------------

def response_high_conf_with_unacknowledged_footnotes() -> dict:
    """Claude returns high-confidence rows despite footnotes being present.
    _validate_footnote_coverage should add a warning flag."""
    return {
        "footnotes": [
            {"ref": "1",
             "text": "Rate subject to monthly volume adjustment by the Exchange.",
             "location": "Page 1 bottom"},
        ],
        "flags": [],
        "rows": [
            {"ticker_class": "Penny", "sec_type": "OPT", "account_type": "CUST",
             "trade_type": "Electronic", "liq_code": "CA",
             "make_rate": -0.01, "take_rate": None, "auction_init_rate": None,
             "auction_resp_rate": None, "breakup_rate": None,
             "source_page": "Page 1", "source_section": "Table 1",
             "footnote_refs": [],         # ← empty despite footnotes existing
             "confidence": "high",        # ← high despite footnotes
             "confidence_reason": None, "notes": None},
        ],
    }


# ---------------------------------------------------------------------------
# Adversarial fixture: duplicate rows (same liq_code returned twice)
# ---------------------------------------------------------------------------

def response_with_duplicate_rows() -> dict:
    """Claude returns the CA row twice; _dedup_rows must catch it."""
    row = {
        "ticker_class": "Penny", "sec_type": "OPT", "account_type": "CUST",
        "trade_type": "Electronic", "liq_code": "CA",
        "make_rate": -0.01, "take_rate": None, "auction_init_rate": None,
        "auction_resp_rate": None, "breakup_rate": None,
        "source_page": "CSV row: CA", "source_section": "Customer adds",
        "footnote_refs": [], "confidence": "medium",
        "confidence_reason": "CBOE CSV omits footnotes.", "notes": None,
    }
    return {"footnotes": [], "flags": [], "rows": [row, row]}


# ---------------------------------------------------------------------------
# Adversarial fixture: null liq_code in CSV extraction
# ---------------------------------------------------------------------------

def response_with_null_liq_codes() -> dict:
    """Claude fails to populate liq_code from the CSV Code column."""
    return {
        "footnotes": [],
        "flags": [],
        "rows": [
            {"ticker_class": "Penny", "sec_type": "OPT", "account_type": "CUST",
             "trade_type": "Electronic", "liq_code": None,   # ← bad
             "make_rate": 0.10, "take_rate": None, "auction_init_rate": None,
             "auction_resp_rate": None, "breakup_rate": None,
             "source_page": "CSV row: ?", "source_section": "Customer Electronic",
             "footnote_refs": [], "confidence": "medium",
             "confidence_reason": "CBOE CSV omits footnotes.", "notes": None},
        ],
    }


# ---------------------------------------------------------------------------
# Temp-DB fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_db(tmp_path):
    """Return a Database instance pointing at a fresh temp SQLite file."""
    from src.persistence.db import Database
    db_file = tmp_path / "test_fees.db"
    return Database(db_path=db_file)
