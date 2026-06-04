#!/usr/bin/env python3
"""
Options Exchange Fee Schedule Automation
Entry point for CLI and scheduler.

Usage:
    python main.py --run-now                     # All exchanges, full pipeline
    python main.py --mock                        # Same pipeline with mock data (no API key)
    python main.py --mock --mock-jitter          # Mock with simulated rate changes (tests alerts)
    python main.py --exchange edgx               # Single exchange
    python main.py --exchange edgx bzx           # Multiple specific exchanges
    python main.py --schedule                    # Start weekly scheduler (blocking)
    python main.py --report                      # Print cross-exchange fee comparison
    python main.py --review                      # Show rows/flags needing human review
    python main.py --footnotes --exchange edgx   # Show footnotes extracted from a schedule
    python main.py --history                     # Print recent run history
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Configure logging before importing anything else
LOG_DIR = Path(os.getenv("LOG_DIR", "logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "fee_automation.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("main")


def cmd_run_now(exchange_ids: list[str] | None, mock: bool, mock_jitter: bool) -> None:
    from src.pipeline import run_all
    run_all(exchange_ids=exchange_ids or None, mock=mock, mock_jitter=mock_jitter)


def cmd_schedule() -> None:
    import schedule as sched
    import time
    import yaml

    with open("config/exchanges.yaml") as f:
        cfg = yaml.safe_load(f)

    settings = cfg.get("settings", {})
    run_schedule = settings.get("run_schedule", "weekly")
    run_day = settings.get("run_day", "monday")
    run_time = settings.get("run_time", "06:00")

    from src.pipeline import run_all

    def job():
        logger.info("Scheduler triggered — starting full pipeline run")
        run_all()

    if run_schedule == "daily":
        sched.every().day.at(run_time).do(job)
        logger.info("Scheduler running daily at %s", run_time)
    else:
        day_fn = getattr(sched.every(), run_day)
        day_fn.at(run_time).do(job)
        logger.info("Scheduler running every %s at %s", run_day, run_time)

    logger.info("Press Ctrl+C to stop")
    while True:
        sched.run_pending()
        time.sleep(60)


def cmd_report(trade_type_filter: str | None = None) -> None:
    from src.persistence.db import Database
    from tabulate import tabulate

    db = Database()
    rows = db.get_all_latest_rows()

    if not rows:
        print("No data in database. Run --run-now or --mock first.")
        return

    sort_key = lambda x: (
        x["exchange_id"],
        x.get("account_type", ""),
        x.get("ticker_class", ""),
        x.get("sec_type", ""),
        x.get("trade_type", ""),
        x.get("liq_code", "") or "",
    )

    table_rows = []
    for r in sorted(rows, key=sort_key):
        if trade_type_filter and r.get("trade_type") != trade_type_filter:
            continue
        table_rows.append([
            r["exchange_id"].upper(),
            r.get("account_type", ""),
            r.get("ticker_class", ""),
            r.get("sec_type", ""),
            r.get("trade_type", ""),
            r.get("liq_code", "") or "",
            _fmt(r.get("make_rate")),
            _fmt(r.get("take_rate")),
            _fmt(r.get("auction_init_rate")),
            _fmt(r.get("auction_resp_rate")),
            _fmt(r.get("breakup_rate")),
        ])

    headers = ["Exchange", "AcctType", "Class", "SecType", "TradeType", "LiqCode",
               "Make", "Take", "AuctInit", "AuctResp", "Breakup"]
    print(tabulate(table_rows, headers=headers, tablefmt="outline"))
    exchange_count = len(set(r["exchange_id"] for r in rows))
    shown = len(table_rows)
    filter_note = f" ({trade_type_filter} only)" if trade_type_filter else ""
    print(f"\n{shown} rows shown{filter_note} | {len(rows)} total rows across {exchange_count} exchange(s)")


def cmd_review() -> None:
    """Print all rows and flags that need human review, with source citations."""
    from src.persistence.db import Database
    from tabulate import tabulate
    import json

    db = Database()

    # Flags first
    flags = db.get_flags()
    if flags:
        print(f"\n{'='*60}")
        print("EXTRACTION FLAGS (AI-identified issues)")
        print('='*60)
        for f in flags:
            print(f"  [{f['severity'].upper()}] {f['exchange_id'].upper()} | {f['location']}")
            print(f"  {f['issue']}")
            print()

    # Low/medium-confidence rows
    rows = db.get_review_needed()
    if not rows and not flags:
        print("Nothing needs review — all rows extracted at high confidence.")
        return

    if rows:
        print(f"\n{'='*60}")
        print("LOW / MEDIUM CONFIDENCE ROWS")
        print('='*60)
        table = []
        for r in rows:
            fn_refs = json.loads(r.get("footnote_refs") or "[]")
            citation = " > ".join(filter(None, [
                r.get("source_page"), r.get("source_section"),
                (f"fn. {', '.join(fn_refs)}" if fn_refs else None),
            ])) or "unknown"
            table.append([
                r["exchange_id"].upper(),
                r.get("confidence", "").upper(),
                r.get("account_type", ""),
                r.get("ticker_class", ""),
                r.get("sec_type", ""),
                r.get("trade_type", ""),
                citation,
                r.get("confidence_reason", "") or "",
            ])
        headers = ["Exchange", "Conf", "AcctType", "Class", "SecType",
                   "TradeType", "Source Citation", "Reason"]
        print(tabulate(table, headers=headers, tablefmt="outline"))

    print(f"\n{len(rows)} row(s) need review | {len(flags)} flag(s) total")


def cmd_footnotes(exchange_id: str) -> None:
    """Print all footnotes extracted from the latest run for an exchange."""
    from src.persistence.db import Database

    db = Database()
    footnotes = db.get_footnotes(exchange_id)
    if not footnotes:
        print(f"No footnotes found for {exchange_id.upper()}. Run --run-now first.")
        return

    print(f"\nFootnotes extracted from {exchange_id.upper()} fee schedule "
          f"({len(footnotes)} total):\n")
    for fn in footnotes:
        print(f"  [{fn['ref']}]  Location: {fn['location']}")
        print(f"       {fn['text']}")
        print()


def cmd_history(limit: int = 20) -> None:
    from src.persistence.db import Database
    from tabulate import tabulate

    db = Database()
    runs = db.get_run_history(limit=limit)

    if not runs:
        print("No run history found.")
        return

    table = [
        [
            r["run_id"],
            r["exchange_id"].upper(),
            r["started_at"][:19],
            r["status"],
            r["row_count"],
            r.get("error_message", "")[:60] if r.get("error_message") else "",
        ]
        for r in runs
    ]
    headers = ["RunID", "Exchange", "Started", "Status", "Rows", "Error"]
    print(tabulate(table, headers=headers, tablefmt="outline"))


def _fmt(val) -> str:
    if val is None:
        return "-"
    v = float(val)
    sign = "+" if v >= 0 else ""
    return f"{sign}${v:.2f}"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Options Exchange Fee Schedule Automation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--run-now",
        action="store_true",
        help="Run the full pipeline immediately (requires ANTHROPIC_API_KEY)",
    )
    group.add_argument(
        "--mock",
        action="store_true",
        help="Run with synthetic mock data — no API key required, tests the full pipeline",
    )
    group.add_argument(
        "--schedule",
        action="store_true",
        help="Start the scheduler (blocking)",
    )
    group.add_argument(
        "--report",
        action="store_true",
        help="Print cross-exchange fee comparison from stored data",
    )
    group.add_argument(
        "--history",
        action="store_true",
        help="Print recent run history",
    )
    group.add_argument(
        "--review",
        action="store_true",
        help="Show rows and flags that need human review, with source citations",
    )
    group.add_argument(
        "--footnotes",
        action="store_true",
        help="Show all footnotes extracted from the latest run (requires --exchange)",
    )

    parser.add_argument(
        "--mock-jitter",
        action="store_true",
        help="With --mock: randomly perturb some rates to simulate changes and trigger alerts",
    )
    parser.add_argument(
        "--exchange",
        nargs="+",
        metavar="ID",
        help="Limit to specific exchange ID(s), e.g. --exchange edgx bzx",
    )
    parser.add_argument(
        "--filter-trade-type",
        choices=["Electronic", "PI", "Solicitation"],
        metavar="TYPE",
        help="Filter --report to one trade type: Electronic, PI, or Solicitation",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.mock_jitter and not args.mock:
        parser.error("--mock-jitter requires --mock")

    if args.run_now:
        cmd_run_now(args.exchange, mock=False, mock_jitter=False)
    elif args.mock:
        cmd_run_now(args.exchange, mock=True, mock_jitter=args.mock_jitter)
    elif args.schedule:
        if args.exchange:
            parser.error("--exchange cannot be used with --schedule")
        cmd_schedule()
    elif args.report:
        cmd_report(trade_type_filter=getattr(args, "filter_trade_type", None))
    elif args.history:
        cmd_history()
    elif args.review:
        cmd_review()
    elif args.footnotes:
        if not args.exchange or len(args.exchange) != 1:
            parser.error("--footnotes requires exactly one --exchange ID")
        cmd_footnotes(args.exchange[0])


if __name__ == "__main__":
    main()
