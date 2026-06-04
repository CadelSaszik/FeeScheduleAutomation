"""Cross-exchange insight generator.

Uses Claude to analyze the full current fee landscape and surface
actionable insights: cheapest venues, recent movers, outliers, etc.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

import anthropic

logger = logging.getLogger(__name__)

MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")
MAX_TOKENS = 4096

_SYSTEM = """You are an expert options market microstructure analyst at a trading firm.
You analyze options exchange fee schedules to identify cost optimization opportunities,
competitive dynamics, and fee trends.

Your audience is quantitative traders and operations staff who already understand
exchange fee structures. Be precise, data-driven, and actionable. Avoid generic
observations — focus on specific numbers, specific venues, and specific comparisons.
"""

_INSIGHT_PROMPT = """
Analyze the current options exchange fee landscape based on the data below.
Surface the top 5-7 most actionable insights for a trading firm, covering:
1. Cheapest venues by capacity type (CUST vs PCUST) and ticker class (Penny vs Non-Penny)
2. Most expensive venues for electronic taker flow
3. Best make rebates available
4. Best AIM/PIP auction economics (init and resp sides)
5. Any notable outliers or unusual pricing relative to peers
6. Any venues where PCUST pricing has advantageous spread vs CUST

Format: numbered list, each insight 1-3 sentences, lead with the specific number or comparison.
Do not include caveats about data accuracy — just analyze what's provided.

Current fee data (JSON):
{fee_data}
"""

_CHANGE_INSIGHT_PROMPT = """
One of the exchanges in the fee landscape just updated its fee schedule.
Based on the change details and the full current fee landscape, generate one concise
insight (2-4 sentences) about what this change means competitively — e.g., how it
repositions the exchange relative to peers, whether it's unusual, what the routing
implication is.

Exchange: {exchange_id}
Change summary:
{change_summary}

Current full landscape (JSON — selected fields):
{landscape_json}
"""


class InsightAnalyzer:
    def __init__(self):
        self.client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    def analyze_landscape(self, all_rows: list[dict]) -> str:
        """Generate a full cross-exchange insight report from all current rows."""
        if not all_rows:
            return "No fee data available for analysis."

        condensed = _condense_rows(all_rows)
        fee_data_str = json.dumps(condensed, indent=2)

        # Truncate if needed
        if len(fee_data_str) > 60_000:
            fee_data_str = fee_data_str[:60_000] + "\n... [truncated]"

        prompt = _INSIGHT_PROMPT.format(fee_data=fee_data_str)

        try:
            response = self.client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text.strip()
        except Exception as exc:
            logger.error("Insight generation failed: %s", exc)
            return f"Insight generation failed: {exc}"

    def analyze_change(
        self,
        exchange_id: str,
        change_summary: str,
        all_rows: list[dict],
    ) -> str:
        """Generate a per-exchange change insight given the diff and full landscape."""
        condensed = _condense_rows(all_rows)
        landscape_json = json.dumps(condensed, indent=2)
        if len(landscape_json) > 40_000:
            landscape_json = landscape_json[:40_000] + "\n... [truncated]"

        prompt = _CHANGE_INSIGHT_PROMPT.format(
            exchange_id=exchange_id.upper(),
            change_summary=change_summary,
            landscape_json=landscape_json,
        )

        try:
            response = self.client.messages.create(
                model=MODEL,
                max_tokens=512,
                system=_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text.strip()
        except Exception as exc:
            logger.error("Change insight generation failed: %s", exc)
            return ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _condense_rows(rows: list[dict]) -> list[dict]:
    """Strip DB metadata fields to reduce token usage."""
    keep = (
        "exchange_id", "ticker_class", "sec_type", "account_type",
        "trade_type", "liq_code", "make_rate", "take_rate",
        "auction_init_rate", "auction_resp_rate", "breakup_rate",
    )
    return [{k: r.get(k) for k in keep} for r in rows]
