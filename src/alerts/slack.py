"""Slack alert delivery via incoming webhook."""

from __future__ import annotations

import logging
import os
from typing import Optional

import requests

from ..diff.engine import DiffReport, RowChange, RateChange

logger = logging.getLogger(__name__)

WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")
TIMEOUT = 10

# Emoji per change type
EMOJI = {
    "added": ":heavy_plus_sign:",
    "removed": ":heavy_minus_sign:",
    "modified": ":pencil2:",
    "ok": ":white_check_mark:",
    "error": ":x:",
    "info": ":information_source:",
}


class SlackAlerter:
    def __init__(self, webhook_url: str = WEBHOOK_URL):
        self.webhook_url = webhook_url

    def is_configured(self) -> bool:
        return bool(self.webhook_url)

    # ------------------------------------------------------------------
    # Public send methods
    # ------------------------------------------------------------------

    def send_diff_report(
        self,
        report: DiffReport,
        insight: Optional[str] = None,
    ) -> bool:
        if not self.is_configured():
            logger.warning("Slack webhook not configured — skipping alert")
            return False

        if not report.has_changes:
            return self._post(self._no_change_payload(report.exchange_id))

        blocks = self._build_diff_blocks(report, insight)
        return self._post({"blocks": blocks})

    def send_error(self, exchange_id: str, error_message: str) -> bool:
        if not self.is_configured():
            return False
        payload = {
            "text": f"{EMOJI['error']} *Fee schedule extraction failed — {exchange_id.upper()}*\n```{error_message}```"
        }
        return self._post(payload)

    def send_run_summary(
        self,
        reports: list[DiffReport],
        errors: list[tuple[str, str]],
        cross_exchange_insight: Optional[str] = None,
    ) -> bool:
        if not self.is_configured():
            return False

        changed = [r for r in reports if r.has_changes]
        unchanged = [r for r in reports if not r.has_changes]

        blocks: list[dict] = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "Options Exchange Fee Schedule Update",
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*{len(changed)}* exchange(s) with changes  |  "
                        f"*{len(unchanged)}* unchanged  |  "
                        f"*{len(errors)}* error(s)"
                    ),
                },
            },
            {"type": "divider"},
        ]

        for report in changed:
            blocks.extend(self._build_diff_blocks(report, insight=None))
            blocks.append({"type": "divider"})

        if errors:
            error_lines = "\n".join(f"• {xid}: {msg}" for xid, msg in errors)
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"{EMOJI['error']} *Extraction errors:*\n{error_lines}",
                    },
                }
            )

        if unchanged:
            ids = ", ".join(r.exchange_id.upper() for r in unchanged)
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"{EMOJI['ok']} *No changes:* {ids}",
                    },
                }
            )

        if cross_exchange_insight:
            blocks.append({"type": "divider"})
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"{EMOJI['info']} *Cross-Exchange Insights:*\n{cross_exchange_insight}",
                    },
                }
            )

        return self._post({"blocks": blocks})

    # ------------------------------------------------------------------
    # Block builders
    # ------------------------------------------------------------------

    def _no_change_payload(self, exchange_id: str) -> dict:
        return {
            "text": f"{EMOJI['ok']} *{exchange_id.upper()}* fee schedule: no changes detected."
        }

    def _build_diff_blocks(
        self,
        report: DiffReport,
        insight: Optional[str],
    ) -> list[dict]:
        blocks: list[dict] = []

        header_text = (
            f"{EMOJI['modified']} *{report.exchange_id.upper()}* — "
            f"{report.total_changes} change(s)"
        )
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": header_text},
            }
        )

        # Modified rows
        if report.modified:
            lines = []
            for chg in report.modified:
                lines.append(f"*{chg.key_label()}*")
                for rc in chg.rate_changes:
                    lines.append(f"  › {rc.fmt()}")
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Rate changes ({len(report.modified)}):*\n" + "\n".join(lines),
                    },
                }
            )

        # Added rows
        if report.added:
            ids = "\n".join(f"• {chg.key_label()}" for chg in report.added[:10])
            if len(report.added) > 10:
                ids += f"\n_...and {len(report.added) - 10} more_"
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"{EMOJI['added']} *New rows ({len(report.added)}):*\n{ids}",
                    },
                }
            )

        # Removed rows
        if report.removed:
            ids = "\n".join(f"• {chg.key_label()}" for chg in report.removed[:10])
            if len(report.removed) > 10:
                ids += f"\n_...and {len(report.removed) - 10} more_"
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"{EMOJI['removed']} *Removed rows ({len(report.removed)}):*\n{ids}",
                    },
                }
            )

        if insight:
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"{EMOJI['info']} _{insight}_",
                    },
                }
            )

        return blocks

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _post(self, payload: dict) -> bool:
        try:
            resp = requests.post(self.webhook_url, json=payload, timeout=TIMEOUT)
            resp.raise_for_status()
            return True
        except Exception as exc:
            logger.error("Slack post failed: %s", exc)
            return False
