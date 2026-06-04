"""Optional email alert delivery."""

from __future__ import annotations

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from ..diff.engine import DiffReport

logger = logging.getLogger(__name__)


class EmailAlerter:
    def __init__(self):
        self.from_addr = os.getenv("EMAIL_FROM", "")
        self.to_addr = os.getenv("EMAIL_TO", "")
        self.smtp_host = os.getenv("EMAIL_SMTP_HOST", "smtp.gmail.com")
        self.smtp_port = int(os.getenv("EMAIL_SMTP_PORT", "587"))
        self.smtp_user = os.getenv("EMAIL_SMTP_USER", "")
        self.smtp_pass = os.getenv("EMAIL_SMTP_PASS", "")

    def is_configured(self) -> bool:
        return bool(self.from_addr and self.to_addr and self.smtp_host)

    def send_diff_report(self, report: DiffReport) -> bool:
        if not self.is_configured():
            logger.debug("Email not configured — skipping")
            return False

        subject = (
            f"[FeeAlert] {report.exchange_id.upper()} — "
            f"{report.total_changes} change(s) detected"
            if report.has_changes
            else f"[FeeAlert] {report.exchange_id.upper()} — No changes"
        )

        body_lines = report.summary_lines()
        body = "\n".join(body_lines)

        return self._send(subject, body)

    def send_run_summary(
        self,
        reports: list[DiffReport],
        errors: list[tuple[str, str]],
    ) -> bool:
        if not self.is_configured():
            return False

        changed = [r for r in reports if r.has_changes]
        subject = (
            f"[FeeAlert] Weekly run — {len(changed)} exchange(s) with changes"
        )

        lines = []
        for r in reports:
            lines.extend(r.summary_lines())
            lines.append("")
        if errors:
            lines.append("ERRORS:")
            for xid, msg in errors:
                lines.append(f"  {xid}: {msg}")

        return self._send(subject, "\n".join(lines))

    def _send(self, subject: str, body: str) -> bool:
        try:
            msg = MIMEMultipart()
            msg["From"] = self.from_addr
            msg["To"] = self.to_addr
            msg["Subject"] = subject
            msg.attach(MIMEText(body, "plain"))

            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                server.starttls()
                if self.smtp_user and self.smtp_pass:
                    server.login(self.smtp_user, self.smtp_pass)
                server.sendmail(self.from_addr, [self.to_addr], msg.as_string())
            logger.info("Email sent to %s", self.to_addr)
            return True
        except Exception as exc:
            logger.error("Email send failed: %s", exc)
            return False
