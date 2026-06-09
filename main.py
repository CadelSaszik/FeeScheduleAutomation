#!/usr/bin/env python3
"""
Options Exchange Fee Schedule Automation
Entry point for CLI and scheduler.

Usage:
    python main.py --preflight                   # Check all endpoints, parse output, no API calls
    python main.py --preflight --exchange edgx   # Preflight a single exchange
    python main.py --run-now                     # All exchanges, full pipeline
    python main.py --mock                        # Same pipeline with mock data (no API key)
    python main.py --mock --mock-jitter          # Mock with simulated rate changes (tests alerts)
    python main.py --exchange edgx               # Single exchange
    python main.py --exchange edgx bzx           # Multiple specific exchanges
    python main.py --schedule                    # Start weekly scheduler (blocking)
    python main.py --report                      # Print cross-exchange fee comparison
    python main.py --report --excel              # Also export to fee_report.xlsx (one sheet/exchange)
    python main.py --report --excel my_fees.xlsx # Export to a named file
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


def cmd_preflight(exchange_ids: list[str] | None) -> None:
    """Fetch and parse every exchange's fee schedule without calling Claude.

    Checks:
      1. HTTP reachability + status code
      2. Content size (sanity — too small = probably an error page)
      3. Text/CSV/HTML extraction succeeds and produces usable content
      4. Last run status from the DB (so you know if a prior real run worked)
    Prints a colour-coded pass/fail table and a preview of extracted content.
    """
    import time
    import yaml
    from src.fetcher import get_fetcher
    from src.pipeline import _find_manual_file, _manual_fetch_result
    from src.persistence.db import Database

    with open("config/exchanges.yaml") as f:
        cfg = yaml.safe_load(f)

    exchanges = [
        ex for ex in cfg["exchanges"]
        if ex.get("enabled", True)
        and (exchange_ids is None or ex["id"] in exchange_ids)
    ]

    if not exchanges:
        print("No matching enabled exchanges found.")
        return

    db = Database()
    # Most-recent run per exchange (run_history is ordered newest-first)
    last_runs: dict[str, dict] = {}
    for r in reversed(db.get_run_history(limit=200)):
        last_runs[r["exchange_id"]] = r

    PASS  = "\033[92mPASS\033[0m"
    WARN  = "\033[93mWARN\033[0m"
    FAIL  = "\033[91mFAIL\033[0m"
    SKIP  = "\033[90mSKIP\033[0m"

    results = []

    for ex in exchanges:
        xid      = ex["id"]
        xname    = ex["name"]
        url      = ex["fee_url"]
        operator = ex["operator"]

        print(f"\n{'-'*70}")
        print(f"  {xname}  ({xid.upper()})")
        print(f"  URL: {url}")

        t0 = time.time()

        # -- Last run from DB ---------------------------------------------
        last = last_runs.get(xid)
        if last:
            status_icon = PASS if last["status"] == "ok" else FAIL
            print(f"  Last run: {status_icon}  {last['started_at'][:19]}  "
                  f"{last['row_count']} rows  "
                  + (f"ERR: {last['error_message'][:60]}" if last.get("error_message") else ""))
        else:
            print(f"  Last run: {SKIP}  (no prior run in DB)")

        # -- Manual file fallback? ----------------------------------------
        manual = _find_manual_file(xid)
        if manual:
            print(f"  Manual file: {manual}  (will be used instead of HTTP fetch)")

        # -- HTTP fetch ---------------------------------------------------
        fetcher_cls = get_fetcher(operator)
        fetcher = fetcher_cls(ex)
        try:
            if manual:
                fetch_result = _manual_fetch_result(xid, operator, ex, manual)
            else:
                fetch_result = fetcher.fetch()
            elapsed = time.time() - t0
        except Exception as exc:
            print(f"  Fetch: {FAIL}  Exception: {exc}")
            results.append((xid, "FAIL", "fetch exception"))
            continue

        if not fetch_result.ok:
            print(f"  Fetch: {FAIL}  HTTP {fetch_result.http_status}  {fetch_result.error or ''}")
            results.append((xid, "FAIL", f"HTTP {fetch_result.http_status}"))
            continue

        size_kb = len(fetch_result.raw_bytes) / 1024
        print(f"  Fetch: {PASS}  HTTP 200  {size_kb:.1f} KB  {elapsed:.2f}s  "
              f"type={fetch_result.content_type}")

        MIN_BYTES = 500
        if len(fetch_result.raw_bytes) < MIN_BYTES:
            print(f"  {WARN}  Response is suspiciously small ({len(fetch_result.raw_bytes)} bytes) "
                  f"— may be an error page")

        # -- Text extraction ----------------------------------------------
        try:
            text = fetcher.extract_text(fetch_result)
        except Exception as exc:
            print(f"  Parse: {FAIL}  Exception during text extraction: {exc}")
            results.append((xid, "FAIL", "parse exception"))
            continue

        if not text or not text.strip():
            print(f"  Parse: {FAIL}  Extraction returned empty text")
            results.append((xid, "FAIL", "empty text"))
            continue

        char_count = len(text)
        line_count = text.count("\n")
        print(f"  Parse: {PASS}  {char_count:,} chars  {line_count:,} lines")

        # -- Content preview (first 8 non-empty lines) --------------------
        preview_lines = [l for l in text.splitlines() if l.strip()][:8]
        print(f"  Preview:")
        for line in preview_lines:
            print(f"    {line[:100]}")

        # -- Heuristic checks ---------------------------------------------
        warnings = []
        text_lower = text.lower()

        # Check for signs the page returned an error/login wall instead of fee data
        for phrase in ("access denied", "403 forbidden", "sign in", "login required",
                       "page not found", "404", "error occurred",
                       "session expiring", "my nasdaq analyst", "listing center"):
            if phrase in text_lower[:2000]:
                warnings.append(f"response may be a JS login wall (contains '{phrase}')")

        # Check for expected fee-related keywords
        fee_keywords = ("transaction fee", "per contract", "rebate", "maker", "taker",
                        "fee", "rate", "customer", "penny", "code")
        fee_hits = sum(1 for kw in fee_keywords if kw in text_lower)
        if fee_hits < 2:
            warnings.append(
                "very few fee-related keywords found — content may be a JS-rendered page "
                "that requires a browser (Playwright) to load actual fee data"
            )

        if fetch_result.content_type == "csv":
            # CSV-specific: check it has at least 3 columns and multiple rows
            lines = [l for l in text.splitlines() if l.strip()]
            col_counts = [len(l.split(",")) for l in lines[:5]]
            if not all(c >= 2 for c in col_counts):
                warnings.append("CSV has fewer than 2 columns — may be malformed")
            if len(lines) < 5:
                warnings.append(f"CSV has only {len(lines)} rows — suspiciously sparse")
            else:
                print(f"  CSV rows: {len(lines)}")

        for w in warnings:
            print(f"  {WARN}  {w}")

        overall = "WARN" if warnings else "PASS"
        results.append((xid, overall, f"{char_count:,} chars"))

    # -- Summary table ----------------------------------------------------
    print(f"\n{'='*70}")
    print("  PREFLIGHT SUMMARY")
    print(f"{'='*70}")
    passed  = [r for r in results if r[1] == "PASS"]
    warned  = [r for r in results if r[1] == "WARN"]
    failed  = [r for r in results if r[1] == "FAIL"]

    for xid, status, detail in results:
        icon = PASS if status == "PASS" else (WARN if status == "WARN" else FAIL)
        print(f"  {icon}  {xid.upper():<12}  {detail}")

    print(f"\n  {len(passed)} PASS  |  {len(warned)} WARN  |  {len(failed)} FAIL  "
          f"out of {len(results)} checked")

    if failed:
        print(f"\n  Fix FAIL exchanges before running --run-now.")
    elif warned:
        print(f"\n  Review WARN exchanges — they fetched OK but content looks unusual.")
    else:
        print(f"\n  All endpoints healthy. Safe to run --run-now.")


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


def cmd_report(trade_type_filter: str | None = None, excel_path: str | None = None) -> None:
    import json as _json
    from src.persistence.db import Database
    from tabulate import tabulate

    db = Database()
    rows = db.get_all_latest_rows()

    if not rows:
        print("No data in database. Run --run-now or --mock first.")
        return

    _sec_order   = {"OPT": 0, "MLEG": 1}
    _acct_order  = {"CUST": 0, "PCUST": 1}
    _trade_order = {"Electronic": 0, "PI": 1, "Solicitation": 2}
    _class_order = {"Penny": 0, "Non-Penny": 1}

    sort_key = lambda x: (
        x.get("exchange_id") or "",
        _sec_order.get(x.get("sec_type") or "", 9),
        _acct_order.get(x.get("account_type") or "", 9),
        _trade_order.get(x.get("trade_type") or "", 9),
        x.get("liq_code") or "",
        _class_order.get(x.get("ticker_class") or "", 9),
    )

    RATE_FIELDS = ["make_rate", "take_rate", "auction_init_rate", "auction_resp_rate", "breakup_rate"]
    headers = ["Exchange", "LiqCode", "SecType", "AcctType", "TradeType", "Class",
               "Make", "Take", "AuctInit", "AuctResp", "Breakup", "Cf", "FnRefs"]

    display_rows = []   # formatted strings for terminal
    raw_rows = []       # raw values for Excel (rates as float or None)

    for r in sorted(rows, key=sort_key):
        if trade_type_filter and r.get("trade_type") != trade_type_filter:
            continue
        fn_refs = _json.loads(r.get("footnote_refs") or "[]")
        conf = r.get("confidence", "high")
        conf_mark = "" if conf == "high" else ("?" if conf == "medium" else "!")
        fn_mark = ",".join(fn_refs) if fn_refs else ""

        meta = [
            (r.get("exchange_id") or "").upper(),
            r.get("liq_code") or "",
            r.get("sec_type") or "",
            r.get("account_type") or "",
            r.get("trade_type") or "",
            r.get("ticker_class") or "",
        ]
        rates_fmt = [_fmt(r.get(f)) for f in RATE_FIELDS]
        rates_raw = [float(r[f]) if r.get(f) is not None else None for f in RATE_FIELDS]
        suffix = [conf_mark, fn_mark]

        display_rows.append(meta + rates_fmt + suffix)
        raw_rows.append(meta + rates_raw + suffix)

    print(tabulate(display_rows, headers=headers, tablefmt="outline"))
    exchange_count = len(set(r["exchange_id"] for r in rows))
    shown = len(display_rows)
    filter_note = f" ({trade_type_filter} only)" if trade_type_filter else ""
    print(f"\n{shown} rows shown{filter_note} | {len(rows)} total rows across {exchange_count} exchange(s)")
    print("Cf: blank=high confidence, ?=medium, !=low  |  FnRefs: applicable footnote IDs")

    if excel_path:
        _write_excel_report(headers, raw_rows, excel_path)
        print(f"\nExcel workbook written to: {excel_path}")


def _write_excel_report(headers: list, raw_rows: list, path: str) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    from collections import defaultdict

    HEADER_FILL = PatternFill("solid", fgColor="1F4E79")
    HEADER_FONT = Font(bold=True, color="FFFFFF", size=10)
    ALT_FILL    = PatternFill("solid", fgColor="D6E4F0")
    RATE_COLS   = {"Make", "Take", "AuctInit", "AuctResp", "Breakup"}
    RATE_FMT    = '#,##0.00;[Red]-#,##0.00'

    wb = Workbook()
    wb.remove(wb.active)  # remove default empty sheet

    # Group rows by exchange (column index 0)
    by_exchange: dict[str, list] = defaultdict(list)
    for row in raw_rows:
        by_exchange[row[0]].append(row)

    def _make_sheet(ws, sheet_rows):
        ws.freeze_panes = "A2"
        # Header
        for col_idx, h in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col_idx, value=h)
            cell.font = HEADER_FONT
            cell.fill = HEADER_FILL
            cell.alignment = Alignment(horizontal="center")
        # Data
        for row_idx, row in enumerate(sheet_rows, 2):
            fill = ALT_FILL if row_idx % 2 == 0 else None
            for col_idx, val in enumerate(row, 1):
                cell = ws.cell(row=row_idx, column=col_idx, value=val)
                if fill:
                    cell.fill = fill
                if headers[col_idx - 1] in RATE_COLS and val is not None:
                    cell.number_format = RATE_FMT
        # Auto-width (cap at 40)
        for col_idx, h in enumerate(headers, 1):
            col_letter = get_column_letter(col_idx)
            max_len = max(
                (len(str(sheet_rows[i][col_idx - 1] or "")) for i in range(len(sheet_rows))),
                default=0,
            )
            ws.column_dimensions[col_letter].width = min(max(max_len, len(h)) + 2, 40)

    # One sheet per exchange
    for xid in sorted(by_exchange):
        ws = wb.create_sheet(title=xid)
        _make_sheet(ws, by_exchange[xid])

    # Summary sheet (all exchanges) at the front
    ws_all = wb.create_sheet(title="All", index=0)
    _make_sheet(ws_all, raw_rows)

    wb.save(path)


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
    import yaml
    from src.persistence.db import Database

    with open("config/exchanges.yaml") as f:
        cfg = yaml.safe_load(f)
    ex = next((e for e in cfg["exchanges"] if e["id"] == exchange_id), None)

    db = Database()
    footnotes = db.get_footnotes(exchange_id)

    # Explain the CBOE CSV footnote limitation
    if not footnotes and ex and ex.get("operator") == "cboe":
        print(
            f"\nNo footnotes stored for {exchange_id.upper()}.\n\n"
            f"CBOE exchanges are fetched via a CSV export endpoint that strips all footnotes\n"
            f"from the underlying fee schedule. The CBOE fee schedule website at\n"
            f"  cboe.com/us/options/membership/fee_schedule/{exchange_id}/\n"
            f"contains footnotes qualifying volume thresholds, program eligibility, and rate\n"
            f"caps that are NOT reflected in the CSV. This is why all CBOE rows are marked\n"
            f"medium confidence — the CSV alone is insufficient for full footnote coverage.\n\n"
            f"To review CBOE footnotes manually, visit the fee schedule page above."
        )
        return

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
        "--preflight",
        action="store_true",
        help="Fetch and parse every endpoint without calling Claude — verify before spending tokens",
    )
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
        "--excel",
        nargs="?",
        const="fee_report.xlsx",
        metavar="FILE",
        help="Write --report output to an Excel workbook with one sheet per exchange (default: fee_report.xlsx)",
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

    if args.preflight:
        cmd_preflight(args.exchange)
    elif args.run_now:
        cmd_run_now(args.exchange, mock=False, mock_jitter=False)
    elif args.mock:
        cmd_run_now(args.exchange, mock=True, mock_jitter=args.mock_jitter)
    elif args.schedule:
        if args.exchange:
            parser.error("--exchange cannot be used with --schedule")
        cmd_schedule()
    elif args.report:
        cmd_report(
            trade_type_filter=getattr(args, "filter_trade_type", None),
            excel_path=getattr(args, "excel", None),
        )
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
