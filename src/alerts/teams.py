"""Microsoft Teams alert delivery via Incoming Webhook.

Uses the Adaptive Card format, which works with both the legacy
Office 365 Connector webhook and the newer Power Automate
'Post adaptive card via webhook' action.

Setup:
  Teams channel → ··· → Connectors → Incoming Webhook → copy URL
  Set TEAMS_WEBHOOK_URL in .env
"""

from __future__ import annotations

import json as _json
import logging
import os
from pathlib import Path
from typing import Optional

import requests

from ..diff.engine import DiffReport

logger = logging.getLogger(__name__)

WEBHOOK_URL = os.getenv("TEAMS_WEBHOOK_URL", "")
TIMEOUT = 10
_DEFAULT_PREVIEW_DIR = Path("data/alert-preview")

# Teams Adaptive Card schema version
_SCHEMA = "http://adaptivecards.io/schemas/adaptive-card.json"
_VERSION = "1.4"


class TeamsAlerter:
    def __init__(
        self,
        webhook_url: str = WEBHOOK_URL,
        dry_run: bool = False,
        preview_dir: Path = _DEFAULT_PREVIEW_DIR,
    ):
        self.webhook_url = webhook_url
        self.dry_run = dry_run
        self.preview_dir = Path(preview_dir)

    def is_configured(self) -> bool:
        return bool(self.webhook_url)

    # ------------------------------------------------------------------
    # Public send methods
    # ------------------------------------------------------------------

    def send_review_needed(
        self,
        exchange_id: str,
        review_summary: str,
    ) -> bool:
        """Post a separate card when extraction flagged items needing human review."""
        if not self.is_configured() and not self.dry_run:
            return False
        if not review_summary:
            return False
        card = _adaptive_card([
            {
                "type": "TextBlock",
                "text": f"⚠️ **{exchange_id.upper()} — Extraction items need review**",
                "weight": "Bolder",
                "color": "Warning",
                "wrap": True,
            },
            {
                "type": "TextBlock",
                "text": review_summary,
                "wrap": True,
                "fontType": "Monospace",
            },
            {
                "type": "TextBlock",
                "text": "Run `python main.py --review` to see full details with source citations.",
                "wrap": True,
                "isSubtle": True,
            },
        ])
        return self._post(card, f"teams_review_{exchange_id}.json")

    def send_diff_report(
        self,
        report: DiffReport,
        insight: Optional[str] = None,
    ) -> bool:
        if not self.is_configured() and not self.dry_run:
            logger.warning("Teams webhook not configured — skipping alert")
            return False

        if not report.has_changes:
            return self._post(_no_change_card(report.exchange_id), f"teams_no_change_{report.exchange_id}.json")

        card = _diff_card(report, insight)
        return self._post(card, f"teams_diff_{report.exchange_id}.json")

    def send_error(self, exchange_id: str, error_message: str) -> bool:
        if not self.is_configured() and not self.dry_run:
            return False
        card = _error_card(exchange_id, error_message)
        return self._post(card, f"teams_error_{exchange_id}.json")

    def send_run_summary(
        self,
        reports: list[DiffReport],
        errors: list[tuple[str, str]],
        cross_exchange_insight: Optional[str] = None,
    ) -> bool:
        if not self.is_configured() and not self.dry_run:
            return False

        changed = [r for r in reports if r.has_changes]
        unchanged = [r for r in reports if not r.has_changes]
        card = _summary_card(changed, unchanged, errors, cross_exchange_insight)
        return self._post(card, "teams_run_summary.json")

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _post(self, card: dict, filename: str) -> bool:
        payload = {
            "type": "message",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": card,
                }
            ],
        }
        if self.dry_run:
            self.preview_dir.mkdir(parents=True, exist_ok=True)
            out = self.preview_dir / filename
            out.write_text(_json.dumps(card, indent=2), encoding="utf-8")
            logger.info("dry_run: Teams card written to %s", out)
            return True
        try:
            resp = requests.post(self.webhook_url, json=payload, timeout=TIMEOUT)
            resp.raise_for_status()
            return True
        except Exception as exc:
            logger.error("Teams post failed: %s", exc)
            return False


# ---------------------------------------------------------------------------
# Card builders
# ---------------------------------------------------------------------------

def _adaptive_card(body: list[dict]) -> dict:
    return {
        "$schema": _SCHEMA,
        "type": "AdaptiveCard",
        "version": _VERSION,
        "body": body,
    }


def _no_change_card(exchange_id: str) -> dict:
    return _adaptive_card([
        {
            "type": "TextBlock",
            "text": f"✅ **{exchange_id.upper()}** — No fee schedule changes detected.",
            "wrap": True,
        }
    ])


def _error_card(exchange_id: str, error_message: str) -> dict:
    return _adaptive_card([
        {
            "type": "TextBlock",
            "text": f"❌ **Fee schedule extraction failed — {exchange_id.upper()}**",
            "color": "Attention",
            "weight": "Bolder",
            "wrap": True,
        },
        {
            "type": "TextBlock",
            "text": error_message,
            "color": "Attention",
            "wrap": True,
            "fontType": "Monospace",
        },
    ])


def _diff_card(report: DiffReport, insight: Optional[str]) -> dict:
    body: list[dict] = [
        {
            "type": "TextBlock",
            "text": (
                f"📝 **{report.exchange_id.upper()}** — "
                f"{report.total_changes} change(s): "
                f"{len(report.added)} added, "
                f"{len(report.removed)} removed, "
                f"{len(report.modified)} modified"
            ),
            "weight": "Bolder",
            "wrap": True,
        }
    ]

    if report.modified:
        lines = []
        for chg in report.modified:
            lines.append(f"**{chg.key_label()}**")
            for rc in chg.rate_changes:
                lines.append(f"→ {rc.fmt()}")
        body.append({
            "type": "TextBlock",
            "text": "\n\n".join(lines),
            "wrap": True,
        })

    if report.added:
        ids = "\n".join(f"+ {chg.key_label()}" for chg in report.added[:10])
        if len(report.added) > 10:
            ids += f"\n...and {len(report.added) - 10} more"
        body.append({
            "type": "TextBlock",
            "text": f"**New rows ({len(report.added)}):**\n{ids}",
            "color": "Good",
            "wrap": True,
        })

    if report.removed:
        ids = "\n".join(f"- {chg.key_label()}" for chg in report.removed[:10])
        if len(report.removed) > 10:
            ids += f"\n...and {len(report.removed) - 10} more"
        body.append({
            "type": "TextBlock",
            "text": f"**Removed rows ({len(report.removed)}):**\n{ids}",
            "color": "Attention",
            "wrap": True,
        })

    if insight:
        body.append({"type": "TextBlock", "text": f"💡 _{insight}_", "wrap": True, "isSubtle": True})

    return _adaptive_card(body)


def _summary_card(
    changed: list[DiffReport],
    unchanged: list[DiffReport],
    errors: list[tuple[str, str]],
    cross_insight: Optional[str],
) -> dict:
    body: list[dict] = [
        {
            "type": "TextBlock",
            "text": "📊 Options Exchange Fee Schedule — Weekly Run",
            "weight": "Bolder",
            "size": "Medium",
        },
        {
            "type": "FactSet",
            "facts": [
                {"title": "Changed", "value": str(len(changed))},
                {"title": "Unchanged", "value": str(len(unchanged))},
                {"title": "Errors", "value": str(len(errors))},
            ],
        },
    ]

    if changed:
        names = ", ".join(r.exchange_id.upper() for r in changed)
        body.append({
            "type": "TextBlock",
            "text": f"**Exchanges with changes:** {names}",
            "wrap": True,
        })
        for report in changed:
            body.extend(_diff_card(report, insight=None)["body"])

    if errors:
        err_text = "\n".join(f"• {xid.upper()}: {msg}" for xid, msg in errors)
        body.append({
            "type": "TextBlock",
            "text": f"**Errors:**\n{err_text}",
            "color": "Attention",
            "wrap": True,
        })

    if unchanged:
        ids = ", ".join(r.exchange_id.upper() for r in unchanged)
        body.append({
            "type": "TextBlock",
            "text": f"✅ No changes: {ids}",
            "color": "Good",
            "wrap": True,
            "isSubtle": True,
        })

    if cross_insight:
        body.append({
            "type": "TextBlock",
            "text": f"💡 **Cross-Exchange Insights:**\n{cross_insight}",
            "wrap": True,
        })

    return _adaptive_card(body)
