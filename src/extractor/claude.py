"""AI extraction engine — two-pass Claude extraction with tool use and prompt caching.

Pass 1: Catalog every footnote (separate API call, result cached for Pass 2).
Pass 2: Extract fee rows with the confirmed footnote list in context.

Tool use enforces the output schema — no text-parsing regex required.
Prompt caching (cache_control: ephemeral) on the system prompt and fee content block
reduces Pass 2 input cost by ~90% on the document tokens.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import anthropic

from .prompts import build_fee_content_block, get_system_prompt

logger = logging.getLogger(__name__)

MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")
MAX_TOKENS = int(os.getenv("CLAUDE_MAX_TOKENS", "16384"))
# Footnote pass only needs enough for the footnotes JSON — cap it to keep costs down.
FOOTNOTE_MAX_TOKENS = 4096

# ---------------------------------------------------------------------------
# Tool schemas — the API enforces these; no JSON regex parsing required.
# ---------------------------------------------------------------------------

FOOTNOTE_TOOL: dict = {
    "name": "record_footnotes",
    "description": (
        "Record every footnote, endnote, asterisk note, dagger note, numbered note, "
        "lettered note, and inline qualifier found in the fee schedule. "
        "Call this after completing Pass 1. If no footnotes exist, pass an empty array."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "footnotes": {
                "type": "array",
                "description": "Every footnote found in the document",
                "items": {
                    "type": "object",
                    "properties": {
                        "ref":      {"type": "string", "description": "Identifier exactly as it appears: '1', '*', '†', 'a', etc."},
                        "text":     {"type": "string", "description": "Full verbatim text of the footnote"},
                        "location": {"type": "string", "description": "Where it appears: 'Page 3 bottom', 'After Table 2', etc."},
                    },
                    "required": ["ref", "text", "location"],
                },
            }
        },
        "required": ["footnotes"],
    },
}

FEE_ROWS_TOOL: dict = {
    "name": "record_fee_rows",
    "description": (
        "Record extracted fee rows and any extraction flags. "
        "Call this after completing Pass 2. Every CUST and PCUST row must be included."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "rows": {
                "type": "array",
                "description": "All extracted CUST and PCUST fee rows",
                "items": {
                    "type": "object",
                    "properties": {
                        "ticker_class":      {"type": ["string", "null"]},
                        "sec_type":          {"type": "string", "enum": ["OPT", "MLEG"]},
                        "account_type":      {"type": "string", "enum": ["CUST", "PCUST"]},
                        "trade_type":        {"type": "string", "enum": ["Electronic", "PI", "Solicitation"]},
                        "liq_code":          {"type": ["string", "null"]},
                        "make_rate":         {"type": ["number", "null"]},
                        "take_rate":         {"type": ["number", "null"]},
                        "auction_init_rate": {"type": ["number", "null"]},
                        "auction_resp_rate": {"type": ["number", "null"]},
                        "breakup_rate":      {"type": ["number", "null"]},
                        "source_page":       {"type": ["string", "null"]},
                        "source_section":    {"type": ["string", "null"]},
                        "footnote_refs":     {"type": "array", "items": {"type": "string"}},
                        "confidence":        {"type": "string", "enum": ["high", "medium", "low"]},
                        "confidence_reason": {"type": ["string", "null"]},
                        "notes":             {"type": ["string", "null"]},
                    },
                    "required": ["sec_type", "account_type", "trade_type", "footnote_refs", "confidence"],
                },
            },
            "flags": {
                "type": "array",
                "description": "Issues that need human review",
                "items": {
                    "type": "object",
                    "properties": {
                        "severity": {"type": "string", "enum": ["warning", "error"]},
                        "location": {"type": "string"},
                        "issue":    {"type": "string"},
                    },
                    "required": ["severity", "location", "issue"],
                },
            },
        },
        "required": ["rows", "flags"],
    },
}


@dataclass
class Footnote:
    ref: str           # "1", "*", "†", "a", etc.
    text: str          # verbatim footnote text
    location: str      # "Page 3 bottom", "After Table 2", etc.

    def as_dict(self) -> dict:
        return {"ref": self.ref, "text": self.text, "location": self.location}


@dataclass
class ExtractionFlag:
    severity: str      # "warning" | "error"
    location: str
    issue: str

    def as_dict(self) -> dict:
        return {"severity": self.severity, "location": self.location, "issue": self.issue}


@dataclass
class FeeRow:
    exchange_id: str
    extracted_at: datetime
    ticker_class: Optional[str]
    sec_type: str               # OPT | MLEG
    account_type: str           # CUST | PCUST
    trade_type: str             # Electronic | PI | Solicitation
    liq_code: Optional[str]
    make_rate: Optional[float]
    take_rate: Optional[float]
    auction_init_rate: Optional[float]
    auction_resp_rate: Optional[float]
    breakup_rate: Optional[float]
    source_page: Optional[str]
    source_section: Optional[str]
    footnote_refs: list[str]
    confidence: str
    confidence_reason: Optional[str]
    notes: Optional[str]

    @property
    def needs_review(self) -> bool:
        return self.confidence in ("medium", "low")

    def citation(self) -> str:
        parts = []
        if self.source_page:
            parts.append(self.source_page)
        if self.source_section:
            parts.append(f'"{self.source_section}"')
        if self.footnote_refs:
            parts.append(f"fn. {', '.join(self.footnote_refs)}")
        return " > ".join(parts) if parts else "unknown"

    def as_dict(self) -> dict:
        return {
            "exchange_id": self.exchange_id,
            "extracted_at": self.extracted_at.isoformat(),
            "ticker_class": self.ticker_class,
            "sec_type": self.sec_type,
            "account_type": self.account_type,
            "trade_type": self.trade_type,
            "liq_code": self.liq_code,
            "make_rate": self.make_rate,
            "take_rate": self.take_rate,
            "auction_init_rate": self.auction_init_rate,
            "auction_resp_rate": self.auction_resp_rate,
            "breakup_rate": self.breakup_rate,
            "source_page": self.source_page,
            "source_section": self.source_section,
            "footnote_refs": json.dumps(self.footnote_refs),
            "confidence": self.confidence,
            "confidence_reason": self.confidence_reason,
            "notes": self.notes,
        }


@dataclass
class ExtractionResult:
    exchange_id: str
    operator: str
    extracted_at: datetime
    rows: list[FeeRow] = field(default_factory=list)
    footnotes: list[Footnote] = field(default_factory=list)
    flags: list[ExtractionFlag] = field(default_factory=list)
    raw_response: str = ""
    error: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def ok(self) -> bool:
        return self.error is None and len(self.rows) > 0

    @property
    def low_confidence_rows(self) -> list[FeeRow]:
        return [r for r in self.rows if r.needs_review]

    def review_summary(self) -> str:
        lines = []
        if self.flags:
            lines.append(f"{len(self.flags)} flag(s) from extraction:")
            for f in self.flags:
                lines.append(f"  [{f.severity.upper()}] {f.location}: {f.issue}")
        lc = self.low_confidence_rows
        if lc:
            lines.append(f"{len(lc)} low/medium-confidence row(s):")
            for r in lc:
                lines.append(
                    f"  [{r.confidence.upper()}] {r.account_type} {r.ticker_class} "
                    f"{r.sec_type} {r.trade_type} — {r.confidence_reason}"
                )
        return "\n".join(lines) if lines else ""


class ClaudeExtractor:
    def __init__(self):
        self.client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    def extract(
        self,
        exchange_id: str,
        operator: str,
        exchange_name: str,
        fee_text: str,
        content_type: str = "text",
        supplemental_text: str = "",
    ) -> ExtractionResult:
        extracted_at = datetime.now(tz=timezone.utc)
        result = ExtractionResult(
            exchange_id=exchange_id,
            operator=operator,
            extracted_at=extracted_at,
        )

        if not fee_text.strip():
            result.error = "Empty fee schedule text — nothing to extract"
            logger.warning("[%s] %s", exchange_id, result.error)
            return result

        system_prompt = get_system_prompt(operator)
        fee_content = build_fee_content_block(
            exchange_name, fee_text, content_type, supplemental_text
        )

        try:
            # Pass 1 — footnote catalog.
            # Skipped for CBOE CSV without supplemental HTML: the CSV itself has no footnote text.
            skip_pass1 = content_type == "csv" and not supplemental_text.strip()
            if skip_pass1:
                footnotes: list[Footnote] = []
                p1_in = p1_out = 0
                logger.info("[%s] Skipping footnote pass (CSV without supplemental)", exchange_id)
            else:
                footnotes, p1_in, p1_out = self._pass1_footnotes(
                    system_prompt, fee_content, exchange_id
                )

            # Pass 2 — row extraction with confirmed footnotes in context.
            rows, flags, p2_in, p2_out = self._pass2_rows(
                system_prompt, fee_content, footnotes, exchange_id, extracted_at
            )

            result.input_tokens = p1_in + p2_in
            result.output_tokens = p1_out + p2_out
            result.raw_response = (
                f"[pass1: {len(footnotes)} footnotes ({p1_in}+{p1_out} tok) | "
                f"pass2: {len(rows)} rows ({p2_in}+{p2_out} tok)]"
            )
            logger.info(
                "[%s] Extraction complete — %d input / %d output tokens (both passes)",
                exchange_id, result.input_tokens, result.output_tokens,
            )

            rows, dedup_flags = _dedup_rows(rows, exchange_id)
            flags.extend(dedup_flags)
            if content_type == "csv":
                flags.extend(_validate_csv_rows(rows, exchange_id))
            flags.extend(_validate_footnote_coverage(rows, footnotes, exchange_id))
            result.rows = rows
            result.footnotes = footnotes
            result.flags = flags

            lc_count = len(result.low_confidence_rows)
            logger.info(
                "[%s] Parsed %d rows (%d need review), %d footnotes, %d flags",
                exchange_id, len(rows), lc_count, len(footnotes), len(flags),
            )
            if lc_count:
                logger.warning("[%s] Review summary:\n%s", exchange_id, result.review_summary())

        except anthropic.APIError as exc:
            result.error = f"Anthropic API error: {exc}"
            logger.error("[%s] %s", exchange_id, result.error)
        except Exception as exc:
            result.error = f"Unexpected extraction error: {exc}"
            logger.exception("[%s] %s", exchange_id, result.error)

        return result

    # ------------------------------------------------------------------
    # Two-pass internals
    # ------------------------------------------------------------------

    def _pass1_footnotes(
        self,
        system_prompt: str,
        fee_content: str,
        exchange_id: str,
    ) -> tuple[list[Footnote], int, int]:
        """Dedicated footnote catalog pass. Returns (footnotes, input_tokens, output_tokens)."""
        logger.info("[%s] Pass 1 — footnote catalog (%s)…", exchange_id, MODEL)
        response = self.client.messages.create(
            model=MODEL,
            max_tokens=FOOTNOTE_MAX_TOKENS,
            system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": fee_content,
                        "cache_control": {"type": "ephemeral"},
                    },
                    {
                        "type": "text",
                        "text": (
                            "PASS 1 — FOOTNOTE CATALOG ONLY.\n\n"
                            "Read the entire fee schedule above. Catalog every footnote, endnote, "
                            "asterisk note, dagger note, numbered note, lettered note, superscript, "
                            "and inline qualifier you find.\n\n"
                            "Call record_footnotes with every footnote found. "
                            "If the document truly contains no footnotes, call record_footnotes "
                            "with an empty array."
                        ),
                    },
                ],
            }],
            tools=[FOOTNOTE_TOOL],
            tool_choice={"type": "tool", "name": "record_footnotes"},
        )
        tool_input = _extract_tool_input(response, "record_footnotes")
        footnotes = _parse_footnotes(tool_input.get("footnotes", []))
        logger.info("[%s] Pass 1 complete — %d footnote(s) found", exchange_id, len(footnotes))
        return footnotes, response.usage.input_tokens, response.usage.output_tokens

    def _pass2_rows(
        self,
        system_prompt: str,
        fee_content: str,
        footnotes: list[Footnote],
        exchange_id: str,
        extracted_at: datetime,
    ) -> tuple[list[FeeRow], list[ExtractionFlag], int, int]:
        """Row extraction pass with the confirmed footnote list in context."""
        logger.info("[%s] Pass 2 — row extraction (%s)…", exchange_id, MODEL)

        if not footnotes:
            fn_block = "  (none — no footnotes were found in this document)"
        else:
            lines = [f"  [{fn.ref}] {fn.text}  ({fn.location})" for fn in footnotes]
            fn_block = "\n".join(lines)

        response = self.client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": fee_content,
                        "cache_control": {"type": "ephemeral"},
                    },
                    {
                        "type": "text",
                        "text": (
                            "PASS 2 — FEE ROW EXTRACTION.\n\n"
                            f"Pass 1 is complete. Confirmed footnotes from this document:\n\n"
                            f"{fn_block}\n\n"
                            "Now extract all CUST and PCUST fee rows. For every row:\n"
                            "- Check each confirmed footnote above — does it apply to this row?\n"
                            "- If yes, add its ref to footnote_refs and explain the condition in "
                            "confidence_reason or notes.\n"
                            "- An empty footnote_refs is only acceptable when you have confirmed "
                            "no footnote from the list above affects this row.\n\n"
                            "Call record_fee_rows with the rows and any flags."
                        ),
                    },
                ],
            }],
            tools=[FEE_ROWS_TOOL],
            tool_choice={"type": "tool", "name": "record_fee_rows"},
        )
        tool_input = _extract_tool_input(response, "record_fee_rows")
        rows = _parse_rows(tool_input.get("rows", []), exchange_id, extracted_at)
        flags = _parse_flags(tool_input.get("flags", []))
        logger.info("[%s] Pass 2 complete — %d row(s), %d flag(s)", exchange_id, len(rows), len(flags))
        return rows, flags, response.usage.input_tokens, response.usage.output_tokens


# ---------------------------------------------------------------------------
# Parsers (exported — used by unit tests)
# ---------------------------------------------------------------------------

def _parse_footnotes(raw: list) -> list[Footnote]:
    footnotes = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        footnotes.append(Footnote(
            ref=str(item.get("ref", "")).strip(),
            text=str(item.get("text", "")).strip(),
            location=str(item.get("location", "")).strip(),
        ))
    return footnotes


def _parse_flags(raw: list) -> list[ExtractionFlag]:
    flags = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        severity = item.get("severity", "warning")
        if severity not in ("warning", "error"):
            severity = "warning"
        flags.append(ExtractionFlag(
            severity=severity,
            location=str(item.get("location", "")).strip(),
            issue=str(item.get("issue", "")).strip(),
        ))
    return flags


def _parse_rows(
    raw_rows: list,
    exchange_id: str,
    extracted_at: datetime,
) -> list[FeeRow]:
    rows: list[FeeRow] = []
    for raw in raw_rows:
        if not isinstance(raw, dict):
            continue
        try:
            fn_refs = raw.get("footnote_refs") or []
            if not isinstance(fn_refs, list):
                fn_refs = [str(fn_refs)] if fn_refs else []

            confidence = raw.get("confidence", "high")
            if confidence not in ("high", "medium", "low"):
                confidence = "medium"

            row = FeeRow(
                exchange_id=exchange_id,
                extracted_at=extracted_at,
                ticker_class=raw.get("ticker_class"),
                sec_type=_validate_enum(raw.get("sec_type"), ["OPT", "MLEG"], "OPT"),
                account_type=_validate_enum(raw.get("account_type"), ["CUST", "PCUST"], "CUST"),
                trade_type=_validate_enum(
                    raw.get("trade_type"),
                    ["Electronic", "PI", "Solicitation"],
                    "Electronic",
                ),
                liq_code=raw.get("liq_code"),
                make_rate=_to_rate(raw.get("make_rate")),
                take_rate=_to_rate(raw.get("take_rate")),
                auction_init_rate=_to_rate(raw.get("auction_init_rate")),
                auction_resp_rate=_to_rate(raw.get("auction_resp_rate")),
                breakup_rate=_to_rate(raw.get("breakup_rate")),
                source_page=raw.get("source_page"),
                source_section=raw.get("source_section"),
                footnote_refs=[str(r).strip() for r in fn_refs],
                confidence=confidence,
                confidence_reason=raw.get("confidence_reason"),
                notes=raw.get("notes"),
            )
            rows.append(row)
        except Exception as exc:
            logger.warning("[%s] Skipping malformed row %s: %s", exchange_id, raw, exc)
    return rows


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_tool_input(response: Any, tool_name: str) -> dict:
    """Extract the input dict from a tool_use response block."""
    for block in (response.content or []):
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == tool_name:
            return block.input or {}
    return {}


def _extract_json(text: str) -> str | None:
    """Extract a JSON object from text. Kept for debugging / external callers."""
    import re
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        return fence.group(1)
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _dedup_rows(
    rows: list[FeeRow], exchange_id: str
) -> tuple[list[FeeRow], list[ExtractionFlag]]:
    seen: dict[tuple, int] = {}
    deduped: list[FeeRow] = []
    flags: list[ExtractionFlag] = []
    for row in rows:
        key = (
            row.ticker_class, row.sec_type, row.account_type,
            row.trade_type, row.liq_code,
        )
        if key in seen:
            seen[key] += 1
        else:
            seen[key] = 0
            deduped.append(row)
    removed = sum(v for v in seen.values())
    if removed:
        logger.warning(
            "[%s] Removed %d duplicate row(s) from extraction output", exchange_id, removed
        )
        flags.append(ExtractionFlag(
            severity="warning",
            location="post-extraction dedup",
            issue=f"{removed} duplicate row(s) removed — Claude returned the same key more than once.",
        ))
    return deduped, flags


def _validate_footnote_coverage(
    rows: list[FeeRow], footnotes: list[Footnote], exchange_id: str
) -> list[ExtractionFlag]:
    if not footnotes:
        return []
    suspicious = [r for r in rows if r.confidence == "high" and not r.footnote_refs]
    if not suspicious:
        return []
    logger.warning(
        "[%s] %d high-confidence row(s) have no footnote_refs but document has %d footnote(s)",
        exchange_id, len(suspicious), len(footnotes),
    )
    return [ExtractionFlag(
        severity="warning",
        location="footnote coverage check",
        issue=(
            f"{len(suspicious)} row(s) are marked high confidence with empty footnote_refs, "
            f"but this document contains {len(footnotes)} footnote(s). "
            f"Verify these rows are genuinely unaffected by any footnote."
        ),
    )]


def _validate_csv_rows(rows: list[FeeRow], exchange_id: str) -> list[ExtractionFlag]:
    flags: list[ExtractionFlag] = []
    null_liq = [r for r in rows if not r.liq_code]
    if null_liq:
        logger.warning(
            "[%s] %d row(s) have null liq_code in CSV extraction — possible consolidation error",
            exchange_id, len(null_liq),
        )
        flags.append(ExtractionFlag(
            severity="error",
            location="liq_code validation",
            issue=(
                f"{len(null_liq)} row(s) have null liq_code. For CSV input every row must carry "
                f"its Code value. Re-extraction may be required."
            ),
        ))
    return flags


def _to_rate(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _validate_enum(value: Any, allowed: list[str], default: str) -> str:
    if value in allowed:
        return value
    return default
