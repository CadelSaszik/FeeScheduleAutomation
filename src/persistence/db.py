"""SQLite persistence layer.

Tables:
  fee_rows          — extracted fee rows with source citations
  footnotes         — every footnote found in each extraction run
  extraction_flags  — AI-flagged issues needing human review
  run_history       — one record per extraction run
  raw_files         — metadata for downloaded raw files
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator, Optional

from ..extractor.claude import ExtractionResult, FeeRow, Footnote, ExtractionFlag

logger = logging.getLogger(__name__)

DB_PATH = Path(os.getenv("DB_PATH", "data/db/fees.db"))


class Database:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ------------------------------------------------------------------
    # Context manager for connections
    # ------------------------------------------------------------------

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS run_history (
                    run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    exchange_id     TEXT NOT NULL,
                    operator        TEXT NOT NULL,
                    started_at      TEXT NOT NULL,
                    finished_at     TEXT,
                    row_count       INTEGER DEFAULT 0,
                    input_tokens    INTEGER DEFAULT 0,
                    output_tokens   INTEGER DEFAULT 0,
                    status          TEXT NOT NULL DEFAULT 'running',
                    error_message   TEXT
                );

                CREATE TABLE IF NOT EXISTS fee_rows (
                    row_id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id              INTEGER NOT NULL REFERENCES run_history(run_id),
                    exchange_id         TEXT NOT NULL,
                    extracted_at        TEXT NOT NULL,
                    ticker_class        TEXT,
                    sec_type            TEXT NOT NULL,
                    account_type        TEXT NOT NULL,
                    trade_type          TEXT NOT NULL,
                    liq_code            TEXT,
                    make_rate           REAL,
                    take_rate           REAL,
                    auction_init_rate   REAL,
                    auction_resp_rate   REAL,
                    breakup_rate        REAL,
                    source_page         TEXT,
                    source_section      TEXT,
                    footnote_refs       TEXT,
                    confidence          TEXT NOT NULL DEFAULT 'high',
                    confidence_reason   TEXT,
                    notes               TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_fee_rows_exchange
                    ON fee_rows(exchange_id, extracted_at);

                CREATE INDEX IF NOT EXISTS idx_fee_rows_confidence
                    ON fee_rows(confidence);

                CREATE TABLE IF NOT EXISTS footnotes (
                    footnote_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id          INTEGER NOT NULL REFERENCES run_history(run_id),
                    exchange_id     TEXT NOT NULL,
                    ref             TEXT NOT NULL,
                    text            TEXT NOT NULL,
                    location        TEXT
                );

                CREATE TABLE IF NOT EXISTS extraction_flags (
                    flag_id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id          INTEGER NOT NULL REFERENCES run_history(run_id),
                    exchange_id     TEXT NOT NULL,
                    severity        TEXT NOT NULL,
                    location        TEXT,
                    issue           TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS raw_files (
                    file_id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id          INTEGER REFERENCES run_history(run_id),
                    exchange_id     TEXT NOT NULL,
                    fetched_at      TEXT NOT NULL,
                    file_path       TEXT NOT NULL,
                    url             TEXT NOT NULL,
                    http_status     INTEGER,
                    content_type    TEXT
                );
            """)
        self._migrate_schema()

    def _migrate_schema(self) -> None:
        """Add any columns missing from pre-existing tables.

        Uses PRAGMA table_info to detect gaps, then ALTER TABLE ADD COLUMN
        for each one. Safe to run on every startup — no-ops when up to date.
        """
        migrations: dict[str, list[tuple[str, str]]] = {
            "fee_rows": [
                ("source_page",       "TEXT"),
                ("source_section",    "TEXT"),
                ("footnote_refs",     "TEXT"),
                ("confidence",        "TEXT NOT NULL DEFAULT 'high'"),
                ("confidence_reason", "TEXT"),
            ],
            "run_history": [
                ("retry_count", "INTEGER NOT NULL DEFAULT 0"),
            ],
        }
        with self._conn() as conn:
            for table, columns in migrations.items():
                existing = {
                    row[1]
                    for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
                }
                for col_name, col_def in columns:
                    if col_name not in existing:
                        conn.execute(
                            f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def}"
                        )
                        logger.info("Migrated %s: added column %s", table, col_name)

    # ------------------------------------------------------------------
    # Run management
    # ------------------------------------------------------------------

    def start_run(self, exchange_id: str, operator: str) -> int:
        now = datetime.now(tz=timezone.utc).isoformat()
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO run_history (exchange_id, operator, started_at, status) VALUES (?,?,?,?)",
                (exchange_id, operator, now, "running"),
            )
            return cur.lastrowid  # type: ignore[return-value]

    def finish_run(self, run_id: int, result: ExtractionResult) -> None:
        now = datetime.now(tz=timezone.utc).isoformat()
        status = "ok" if result.ok else "error"
        with self._conn() as conn:
            conn.execute(
                """UPDATE run_history
                   SET finished_at=?, row_count=?, input_tokens=?, output_tokens=?,
                       status=?, error_message=?, retry_count=?
                   WHERE run_id=?""",
                (now, len(result.rows), result.input_tokens, result.output_tokens,
                 status, result.error, result.retry_count, run_id),
            )

    # ------------------------------------------------------------------
    # Storing extraction output
    # ------------------------------------------------------------------

    def save_rows(self, run_id: int, rows: list[FeeRow]) -> None:
        if not rows:
            return
        with self._conn() as conn:
            conn.executemany(
                """INSERT INTO fee_rows
                   (run_id, exchange_id, extracted_at, ticker_class, sec_type,
                    account_type, trade_type, liq_code, make_rate, take_rate,
                    auction_init_rate, auction_resp_rate, breakup_rate,
                    source_page, source_section, footnote_refs,
                    confidence, confidence_reason, notes)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        run_id, r.exchange_id, r.extracted_at.isoformat(),
                        r.ticker_class, r.sec_type, r.account_type, r.trade_type,
                        r.liq_code, r.make_rate, r.take_rate,
                        r.auction_init_rate, r.auction_resp_rate, r.breakup_rate,
                        r.source_page, r.source_section,
                        json.dumps(r.footnote_refs),
                        r.confidence, r.confidence_reason, r.notes,
                    )
                    for r in rows
                ],
            )

    def save_footnotes(self, run_id: int, footnotes: list[Footnote]) -> None:
        if not footnotes:
            return
        exchange_id = _run_exchange_id(self, run_id)
        with self._conn() as conn:
            conn.executemany(
                """INSERT INTO footnotes (run_id, exchange_id, ref, text, location)
                   VALUES (?,?,?,?,?)""",
                [(run_id, exchange_id, fn.ref, fn.text, fn.location) for fn in footnotes],
            )

    def save_flags(self, run_id: int, flags: list[ExtractionFlag]) -> None:
        if not flags:
            return
        exchange_id = _run_exchange_id(self, run_id)
        with self._conn() as conn:
            conn.executemany(
                """INSERT INTO extraction_flags (run_id, exchange_id, severity, location, issue)
                   VALUES (?,?,?,?,?)""",
                [(run_id, exchange_id, f.severity, f.location, f.issue) for f in flags],
            )

    def save_raw_file(
        self,
        run_id: int,
        exchange_id: str,
        fetched_at: str,
        file_path: str,
        url: str,
        http_status: int,
        content_type: str,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO raw_files
                   (run_id, exchange_id, fetched_at, file_path, url, http_status, content_type)
                   VALUES (?,?,?,?,?,?,?)""",
                (run_id, exchange_id, fetched_at, file_path, url, http_status, content_type),
            )

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_latest_rows(self, exchange_id: str) -> list[dict]:
        with self._conn() as conn:
            run = conn.execute(
                """SELECT run_id FROM run_history
                   WHERE exchange_id=? AND status='ok'
                   ORDER BY finished_at DESC LIMIT 1""",
                (exchange_id,),
            ).fetchone()
            if not run:
                return []
            rows = conn.execute(
                "SELECT * FROM fee_rows WHERE run_id=?", (run["run_id"],)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_previous_rows(self, exchange_id: str, before_run_id: int) -> list[dict]:
        with self._conn() as conn:
            run = conn.execute(
                """SELECT run_id FROM run_history
                   WHERE exchange_id=? AND status='ok' AND run_id < ?
                   ORDER BY run_id DESC LIMIT 1""",
                (exchange_id, before_run_id),
            ).fetchone()
            if not run:
                return []
            rows = conn.execute(
                "SELECT * FROM fee_rows WHERE run_id=?", (run["run_id"],)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_all_latest_rows(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT fr.*
                   FROM fee_rows fr
                   JOIN (
                       SELECT exchange_id, MAX(run_id) AS max_run
                       FROM run_history WHERE status='ok'
                       GROUP BY exchange_id
                   ) latest ON fr.run_id = latest.max_run
                       AND fr.exchange_id = latest.exchange_id""",
            ).fetchall()
            return [dict(r) for r in rows]

    def get_all_latest_footnotes(self) -> dict[tuple[str, str], str]:
        """Return {(exchange_id, ref): text} for each exchange's most recent run that
        actually contains footnotes.  Falls back gracefully when a newer run stored no
        footnotes (e.g. the HTML supplemental fetch failed during that run)."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT fn.exchange_id, fn.ref, fn.text
                   FROM footnotes fn
                   JOIN (
                       SELECT fn2.exchange_id, MAX(fn2.run_id) AS max_run
                       FROM footnotes fn2
                       JOIN run_history rh ON fn2.run_id = rh.run_id
                       WHERE rh.status = 'ok'
                       GROUP BY fn2.exchange_id
                   ) latest ON fn.run_id = latest.max_run
                       AND fn.exchange_id = latest.exchange_id""",
            ).fetchall()
        return {(r["exchange_id"], r["ref"]): r["text"] for r in rows}

    def get_footnotes(self, exchange_id: str, run_id: Optional[int] = None) -> list[dict]:
        """Return footnotes for a specific run_id, or the most recent run that has
        footnotes for exchange_id.  Falls back to any run with footnotes rather than
        returning empty when the latest run had no supplemental HTML."""
        with self._conn() as conn:
            if run_id is None:
                run = conn.execute(
                    """SELECT fn.run_id FROM footnotes fn
                       JOIN run_history rh ON fn.run_id = rh.run_id
                       WHERE fn.exchange_id=? AND rh.status='ok'
                       GROUP BY fn.run_id
                       ORDER BY fn.run_id DESC LIMIT 1""",
                    (exchange_id,),
                ).fetchone()
                if not run:
                    return []
                run_id = run["run_id"]
            rows = conn.execute(
                "SELECT * FROM footnotes WHERE run_id=? ORDER BY ref",
                (run_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_flags(self, exchange_id: Optional[str] = None, limit: int = 100) -> list[dict]:
        """Return recent extraction flags, optionally filtered by exchange."""
        with self._conn() as conn:
            if exchange_id:
                rows = conn.execute(
                    """SELECT ef.*, rh.started_at FROM extraction_flags ef
                       JOIN run_history rh ON ef.run_id = rh.run_id
                       WHERE ef.exchange_id=?
                       ORDER BY rh.started_at DESC LIMIT ?""",
                    (exchange_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT ef.*, rh.started_at FROM extraction_flags ef
                       JOIN run_history rh ON ef.run_id = rh.run_id
                       ORDER BY rh.started_at DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]

    def get_review_needed(self) -> list[dict]:
        """Return all medium/low-confidence rows from the latest run of each exchange."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT fr.*
                   FROM fee_rows fr
                   JOIN (
                       SELECT exchange_id, MAX(run_id) AS max_run
                       FROM run_history WHERE status='ok'
                       GROUP BY exchange_id
                   ) latest ON fr.run_id = latest.max_run
                       AND fr.exchange_id = latest.exchange_id
                   WHERE fr.confidence IN ('medium', 'low')
                   ORDER BY fr.exchange_id, fr.confidence DESC""",
            ).fetchall()
            return [dict(r) for r in rows]

    def get_run_history(self, limit: int = 50) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM run_history ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_exchange_id(db: Database, run_id: int) -> str:
    with db._conn() as conn:
        row = conn.execute(
            "SELECT exchange_id FROM run_history WHERE run_id=?", (run_id,)
        ).fetchone()
        return row["exchange_id"] if row else ""
