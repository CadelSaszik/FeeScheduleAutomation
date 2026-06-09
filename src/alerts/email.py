"""Optional email alert delivery."""

from __future__ import annotations

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

from ..diff.engine import DiffReport

logger = logging.getLogger(__name__)

_DEFAULT_PREVIEW_DIR = Path("data/alert-preview")


class EmailAlerter:
    def __init__(
        self,
        dry_run: bool = False,
        preview_dir: Path = _DEFAULT_PREVIEW_DIR,
    ):
        self.from_addr = os.getenv("EMAIL_FROM", "")
        self.to_addr = os.getenv("EMAIL_TO", "")
        self.smtp_host = os.getenv("EMAIL_SMTP_HOST", "smtp.gmail.com")
        self.smtp_port = int(os.getenv("EMAIL_SMTP_PORT", "587"))
        self.smtp_user = os.getenv("EMAIL_SMTP_USER", "")
        self.smtp_pass = os.getenv("EMAIL_SMTP_PASS", "")
        self.dry_run = dry_run
        self.preview_dir = Path(preview_dir)

    def is_configured(self) -> bool:
        return bool(self.from_addr and self.to_addr and self.smtp_host)

    def send_diff_report(self, report: DiffReport) -> bool:
        if not self.is_configured() and not self.dry_run:
            logger.debug("Email not configured — skipping")
            return False

        subject = (
            f"[FeeAlert] {report.exchange_id.upper()} — "
            f"{report.total_changes} change(s) detected"
            if report.has_changes
            else f"[FeeAlert] {report.exchange_id.upper()} — No changes"
        )
        body = "\n".join(report.summary_lines())
        return self._send(subject, body, f"email_diff_{report.exchange_id}.txt")

    def send_run_summary(
        self,
        reports: list[DiffReport],
        errors: list[tuple[str, str]],
    ) -> bool:
        if not self.is_configured() and not self.dry_run:
            return False

        changed = [r for r in reports if r.has_changes]
        subject = f"[FeeAlert] Weekly run — {len(changed)} exchange(s) with changes"

        lines = []
        for r in reports:
            lines.extend(r.summary_lines())
            lines.append("")
        if errors:
            lines.append("ERRORS:")
            for xid, msg in errors:
                lines.append(f"  {xid}: {msg}")

        return self._send(subject, "\n".join(lines), "email_run_summary.txt")

    def _send(self, subject: str, body: str, filename: str) -> bool:
        if self.dry_run:
            self.preview_dir.mkdir(parents=True, exist_ok=True)
            out = self.preview_dir / filename
            out.write_text(
                f"Subject: {subject}\nTo: {self.to_addr or '(not configured)'}\n\n{body}",
                encoding="utf-8",
            )
            logger.info("dry_run: email written to %s", out)
            return True
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
