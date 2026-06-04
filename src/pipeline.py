"""Core pipeline: fetch → extract → diff → alert for one or all exchanges."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

import yaml
from dotenv import load_dotenv

load_dotenv()

from .fetcher import get_fetcher
from .extractor.claude import ClaudeExtractor, ExtractionResult
from .extractor.mock import MockExtractor
from .persistence.db import Database
from .diff.engine import DiffEngine, DiffReport
from .alerts.teams import TeamsAlerter
from .alerts.email import EmailAlerter
from .insights.analyzer import InsightAnalyzer

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "config/exchanges.yaml"))

Extractor = Union[ClaudeExtractor, MockExtractor]


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def run_exchange(
    exchange_cfg: dict,
    db: Database,
    extractor: Extractor,
    diff_engine: DiffEngine,
    teams: TeamsAlerter,
    email: EmailAlerter,
    analyzer: Optional[InsightAnalyzer],
    all_rows_for_insight: Optional[list[dict]] = None,
) -> tuple[Optional[DiffReport], Optional[str]]:
    """Run the full pipeline for a single exchange. Returns (DiffReport, error_message)."""
    exchange_id = exchange_cfg["id"]
    operator = exchange_cfg["operator"]
    exchange_name = exchange_cfg["name"]
    is_mock = isinstance(extractor, MockExtractor)

    logger.info("=== Starting run for %s (%s)%s ===",
                exchange_name, exchange_id, " [MOCK]" if is_mock else "")

    # 1. Start run record
    run_id = db.start_run(exchange_id, operator)

    # 2. Fetch (skip in mock mode — no real file needed)
    fee_text = ""
    if is_mock:
        fee_text = "[mock]"
        db.save_raw_file(
            run_id=run_id,
            exchange_id=exchange_id,
            fetched_at=datetime.now(tz=timezone.utc).isoformat(),
            file_path="[mock]",
            url=exchange_cfg["fee_url"],
            http_status=200,
            content_type=exchange_cfg["schedule_type"],
        )
    else:
        fetcher_cls = get_fetcher(operator)
        fetcher = fetcher_cls(exchange_cfg)
        fetch_result = fetcher.fetch()

        db.save_raw_file(
            run_id=run_id,
            exchange_id=exchange_id,
            fetched_at=fetch_result.fetched_at.isoformat(),
            file_path=str(fetch_result.file_path),
            url=fetch_result.url,
            http_status=fetch_result.http_status,
            content_type=fetch_result.content_type,
        )

        if not fetch_result.ok:
            error_msg = fetch_result.error or f"HTTP {fetch_result.http_status}"
            logger.error("[%s] Fetch failed: %s", exchange_id, error_msg)
            dummy = ExtractionResult(
                exchange_id=exchange_id, operator=operator,
                extracted_at=datetime.now(tz=timezone.utc), error=error_msg,
            )
            db.finish_run(run_id, dummy)
            teams.send_error(exchange_id, error_msg)
            return None, error_msg

        fee_text = fetcher.extract_text(fetch_result)
        if not fee_text.strip():
            error_msg = "Could not extract text from downloaded file"
            logger.error("[%s] %s", exchange_id, error_msg)
            dummy = ExtractionResult(
                exchange_id=exchange_id, operator=operator,
                extracted_at=datetime.now(tz=timezone.utc), error=error_msg,
            )
            db.finish_run(run_id, dummy)
            teams.send_error(exchange_id, error_msg)
            return None, error_msg

    # 3. AI (or mock) extraction
    extraction = extractor.extract(exchange_id, operator, exchange_name, fee_text)
    db.finish_run(run_id, extraction)

    if not extraction.ok:
        logger.error("[%s] Extraction failed: %s", exchange_id, extraction.error)
        teams.send_error(exchange_id, extraction.error or "Unknown extraction error")
        return None, extraction.error

    # 4. Save rows, footnotes, and flags
    db.save_rows(run_id, extraction.rows)
    db.save_footnotes(run_id, extraction.footnotes)
    db.save_flags(run_id, extraction.flags)

    # Log review items immediately so they appear in the run log
    review = extraction.review_summary()
    if review:
        logger.warning("[%s] Items needing review:\n%s", exchange_id, review)

    # 5. Diff
    old_rows = db.get_previous_rows(exchange_id, before_run_id=run_id)
    new_rows = [r.as_dict() for r in extraction.rows]
    report = diff_engine.diff(exchange_id, old_rows, new_rows)

    # 6. Generate per-exchange change insight (skipped in mock mode)
    insight: Optional[str] = None
    if report.has_changes and analyzer is not None and all_rows_for_insight is not None:
        change_summary = "\n".join(report.summary_lines())
        insight = analyzer.analyze_change(exchange_id, change_summary, all_rows_for_insight)
        if insight:
            logger.info("[%s] Change insight: %s", exchange_id, insight[:120])

    # 7. Alert
    teams.send_diff_report(report, insight=insight)
    email.send_diff_report(report)

    # Fire a separate review alert if anything needs human verification
    review = extraction.review_summary()
    if review:
        teams.send_review_needed(exchange_id, review)

    return report, None


def run_all(
    exchange_ids: Optional[list[str]] = None,
    mock: bool = False,
    mock_jitter: bool = False,
) -> None:
    """Run the full pipeline for all enabled exchanges (or a filtered subset).

    Args:
        exchange_ids: If set, only process these exchange IDs.
        mock: Use MockExtractor instead of Claude — no API key required.
        mock_jitter: When mocking, randomly perturb a few rates so the diff
                     engine fires alerts (useful for testing alert delivery).
    """
    cfg = load_config()
    db = Database()
    extractor: Extractor = MockExtractor(jitter=mock_jitter) if mock else ClaudeExtractor()
    diff_engine = DiffEngine()
    teams = TeamsAlerter()
    email_alerter = EmailAlerter()
    # Skip the insight analyzer in mock mode (no API key)
    analyzer: Optional[InsightAnalyzer] = None if mock else InsightAnalyzer()

    exchanges = [
        ex for ex in cfg["exchanges"]
        if ex.get("enabled", True)
        and (exchange_ids is None or ex["id"] in exchange_ids)
    ]

    if not exchanges:
        logger.warning("No matching enabled exchanges found")
        return

    logger.info(
        "Running pipeline for %d exchange(s)%s…",
        len(exchanges), " [MOCK MODE]" if mock else "",
    )

    all_rows = db.get_all_latest_rows()

    reports: list[DiffReport] = []
    errors: list[tuple[str, str]] = []

    for ex_cfg in exchanges:
        report, error = run_exchange(
            exchange_cfg=ex_cfg,
            db=db,
            extractor=extractor,
            diff_engine=diff_engine,
            teams=teams,
            email=email_alerter,
            analyzer=analyzer,
            all_rows_for_insight=all_rows,
        )
        if report is not None:
            reports.append(report)
        if error is not None:
            errors.append((ex_cfg["id"], error))

    # Cross-exchange insight (only when real extraction ran multiple exchanges)
    cross_insight: Optional[str] = None
    if not mock and len(exchanges) > 3 and analyzer is not None:
        fresh_rows = db.get_all_latest_rows()
        cross_insight = analyzer.analyze_landscape(fresh_rows)

    # Consolidated run summary
    if len(exchanges) > 1:
        teams.send_run_summary(reports, errors, cross_exchange_insight=cross_insight)
        email_alerter.send_run_summary(reports, errors)

    logger.info(
        "Pipeline complete — %d exchange(s) processed, %d error(s)",
        len(exchanges), len(errors),
    )
