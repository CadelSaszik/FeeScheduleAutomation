"""Alert dry-run and preview-alerts tests.

Tracer bullet → per-channel send methods → cmd_preview_alerts integration.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from src.alerts.teams import TeamsAlerter
from src.alerts.email import EmailAlerter
from src.diff.engine import DiffReport, RowChange, RateChange


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

def _report_with_changes(exchange_id: str = "edgx") -> DiffReport:
    return DiffReport(
        exchange_id=exchange_id,
        has_changes=True,
        modified=[
            RowChange(
                key={
                    "exchange_id": exchange_id,
                    "ticker_class": "Penny",
                    "sec_type": "OPT",
                    "account_type": "CUST",
                    "trade_type": "Electronic",
                    "liq_code": "CA",
                },
                change_type="modified",
                rate_changes=[
                    RateChange(
                        field="make_rate",
                        field_label="Make Rate",
                        old_value=-0.48,
                        new_value=-0.50,
                    )
                ],
            )
        ],
        added=[
            RowChange(
                key={
                    "exchange_id": exchange_id,
                    "ticker_class": "Non-Penny",
                    "sec_type": "OPT",
                    "account_type": "CUST",
                    "trade_type": "Electronic",
                    "liq_code": "NA",
                },
                change_type="added",
            )
        ],
    )


def _report_no_changes(exchange_id: str = "bzx") -> DiffReport:
    return DiffReport(exchange_id=exchange_id, has_changes=False)


# ---------------------------------------------------------------------------
# TeamsAlerter dry-run
# ---------------------------------------------------------------------------

class TestTeamsDryRun:
    def test_diff_with_changes_writes_file_not_http(self, tmp_path):
        """Tracer bullet: dry_run writes JSON, never calls requests.post."""
        alerter = TeamsAlerter(webhook_url="https://fake.hook", dry_run=True, preview_dir=tmp_path)
        with patch("requests.post") as mock_post:
            result = alerter.send_diff_report(_report_with_changes("edgx"))
        mock_post.assert_not_called()
        assert result is True
        out = tmp_path / "teams_diff_edgx.json"
        assert out.exists()
        payload = json.loads(out.read_text())
        assert payload["type"] == "message"
        card = payload["attachments"][0]["content"]
        assert card["type"] == "AdaptiveCard"

    def test_diff_no_changes_writes_distinct_file(self, tmp_path):
        alerter = TeamsAlerter(webhook_url="https://fake.hook", dry_run=True, preview_dir=tmp_path)
        with patch("requests.post") as mock_post:
            result = alerter.send_diff_report(_report_no_changes("bzx"))
        mock_post.assert_not_called()
        assert result is True
        assert (tmp_path / "teams_no_change_bzx.json").exists()

    def test_error_card_writes_file(self, tmp_path):
        alerter = TeamsAlerter(webhook_url="https://fake.hook", dry_run=True, preview_dir=tmp_path)
        with patch("requests.post") as mock_post:
            result = alerter.send_error("edgx", "Connection timeout after 10s")
        mock_post.assert_not_called()
        assert result is True
        assert (tmp_path / "teams_error_edgx.json").exists()

    def test_run_summary_writes_file(self, tmp_path):
        alerter = TeamsAlerter(webhook_url="https://fake.hook", dry_run=True, preview_dir=tmp_path)
        reports = [_report_with_changes("edgx"), _report_no_changes("bzx")]
        with patch("requests.post") as mock_post:
            result = alerter.send_run_summary(reports, errors=[("c2", "timeout")])
        mock_post.assert_not_called()
        assert result is True
        assert (tmp_path / "teams_run_summary.json").exists()

    def test_review_needed_writes_file(self, tmp_path):
        alerter = TeamsAlerter(webhook_url="https://fake.hook", dry_run=True, preview_dir=tmp_path)
        with patch("requests.post") as mock_post:
            result = alerter.send_review_needed("edgx", "3 low-confidence rows need review")
        mock_post.assert_not_called()
        assert result is True
        assert (tmp_path / "teams_review_edgx.json").exists()

    def test_dry_run_works_without_webhook_url(self, tmp_path):
        """dry_run should bypass the is_configured() guard."""
        alerter = TeamsAlerter(webhook_url="", dry_run=True, preview_dir=tmp_path)
        result = alerter.send_diff_report(_report_with_changes("edgx"))
        assert result is True
        assert (tmp_path / "teams_diff_edgx.json").exists()

    def test_normal_mode_still_calls_http(self, tmp_path):
        """Ensure we haven't broken the live path."""
        alerter = TeamsAlerter(webhook_url="https://fake.hook", dry_run=False, preview_dir=tmp_path)
        mock_resp = type("R", (), {"raise_for_status": lambda s: None})()
        with patch("requests.post", return_value=mock_resp) as mock_post:
            alerter.send_diff_report(_report_with_changes("edgx"))
        mock_post.assert_called_once()


# ---------------------------------------------------------------------------
# EmailAlerter dry-run
# ---------------------------------------------------------------------------

class TestEmailDryRun:
    def test_diff_report_writes_file_not_smtp(self, tmp_path):
        alerter = EmailAlerter(dry_run=True, preview_dir=tmp_path)
        with patch("smtplib.SMTP") as mock_smtp:
            result = alerter.send_diff_report(_report_with_changes("edgx"))
        mock_smtp.assert_not_called()
        assert result is True
        out = tmp_path / "email_diff_edgx.txt"
        assert out.exists()
        content = out.read_text()
        assert "edgx" in content.lower()
        assert "Subject:" in content

    def test_run_summary_writes_file(self, tmp_path):
        alerter = EmailAlerter(dry_run=True, preview_dir=tmp_path)
        reports = [_report_with_changes("edgx"), _report_no_changes("bzx")]
        with patch("smtplib.SMTP") as mock_smtp:
            result = alerter.send_run_summary(reports, errors=[])
        mock_smtp.assert_not_called()
        assert result is True
        assert (tmp_path / "email_run_summary.txt").exists()

    def test_dry_run_works_without_smtp_config(self, tmp_path):
        """dry_run bypasses is_configured() for email too."""
        alerter = EmailAlerter(dry_run=True, preview_dir=tmp_path)
        result = alerter.send_diff_report(_report_with_changes("edgx"))
        assert result is True

    def test_normal_mode_skips_when_not_configured(self, tmp_path):
        """Live path with no config should still silently skip."""
        alerter = EmailAlerter(dry_run=False, preview_dir=tmp_path)
        result = alerter.send_diff_report(_report_with_changes("edgx"))
        assert result is False


# ---------------------------------------------------------------------------
# cmd_preview_alerts integration
# ---------------------------------------------------------------------------

class TestPreviewAlerts:
    def test_creates_all_expected_files(self, tmp_path):
        from main import cmd_preview_alerts
        cmd_preview_alerts(preview_dir=tmp_path)
        expected = [
            "teams_diff_edgx.json",
            "teams_no_change_bzx.json",
            "teams_error_c2.json",
            "teams_run_summary.json",
            "teams_review_edgx.json",
            "email_diff_edgx.txt",
            "email_run_summary.txt",
        ]
        for fname in expected:
            assert (tmp_path / fname).exists(), f"Missing preview file: {fname}"

    def test_all_teams_files_are_valid_adaptive_cards(self, tmp_path):
        from main import cmd_preview_alerts
        cmd_preview_alerts(preview_dir=tmp_path)
        teams_files = list(tmp_path.glob("teams_*.json"))
        assert teams_files, "No teams_*.json files written"
        for f in teams_files:
            payload = json.loads(f.read_text())
            card = payload["attachments"][0]["content"]
            assert card["type"] == "AdaptiveCard", f"{f.name}: expected AdaptiveCard, got {card.get('type')}"
            assert "body" in card, f"{f.name}: AdaptiveCard missing body"
