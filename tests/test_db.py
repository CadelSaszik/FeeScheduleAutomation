"""Database persistence round-trip tests.

All tests use a temp SQLite file (via tmp_db fixture from conftest.py) so they
are isolated and leave no artefacts.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from src.extractor.claude import ExtractionFlag, ExtractionResult, FeeRow, Footnote
from tests.conftest import make_footnote, make_row, NOW


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _finish(db, run_id: int, rows, footnotes=None, flags=None):
    """Save rows, footnotes, flags and mark a run as complete."""
    result = ExtractionResult(
        exchange_id="edgx", operator="cboe", extracted_at=NOW,
        rows=rows, footnotes=footnotes or [], flags=flags or [],
        input_tokens=100, output_tokens=50,
    )
    db.finish_run(run_id, result)
    db.save_rows(run_id, rows)
    if footnotes:
        db.save_footnotes(run_id, footnotes)
    if flags:
        db.save_flags(run_id, flags)
    return result


# ===========================================================================
# Schema
# ===========================================================================

class TestSchema:
    def test_tables_created_on_init(self, tmp_db):
        import sqlite3
        conn = sqlite3.connect(tmp_db.db_path)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        conn.close()
        assert {"fee_rows", "footnotes", "extraction_flags", "run_history", "raw_files"} <= tables

    def test_migration_idempotent(self, tmp_db):
        """Calling _migrate_schema twice must not raise or duplicate columns."""
        from src.persistence.db import Database
        db2 = Database(db_path=tmp_db.db_path)
        db2._migrate_schema()  # second call — should be a no-op


# ===========================================================================
# Run management
# ===========================================================================

class TestRunManagement:
    def test_start_run_returns_positive_id(self, tmp_db):
        run_id = tmp_db.start_run("edgx", "cboe")
        assert isinstance(run_id, int)
        assert run_id > 0

    def test_start_run_status_is_running(self, tmp_db):
        run_id = tmp_db.start_run("edgx", "cboe")
        history = tmp_db.get_run_history(limit=1)
        assert history[0]["status"] == "running"
        assert history[0]["run_id"] == run_id

    def test_finish_run_sets_status_ok(self, tmp_db):
        run_id = tmp_db.start_run("edgx", "cboe")
        rows = [make_row()]
        _finish(tmp_db, run_id, rows)
        history = tmp_db.get_run_history(limit=1)
        assert history[0]["status"] == "ok"
        assert history[0]["row_count"] == 1

    def test_finish_run_error_sets_status_error(self, tmp_db):
        run_id = tmp_db.start_run("edgx", "cboe")
        result = ExtractionResult(
            exchange_id="edgx", operator="cboe", extracted_at=NOW,
            error="API failure",
        )
        tmp_db.finish_run(run_id, result)
        history = tmp_db.get_run_history(limit=1)
        assert history[0]["status"] == "error"

    def test_multiple_runs_ordered_newest_first(self, tmp_db):
        for _ in range(3):
            run_id = tmp_db.start_run("edgx", "cboe")
            _finish(tmp_db, run_id, [make_row()])
        history = tmp_db.get_run_history(limit=10)
        ids = [r["run_id"] for r in history]
        assert ids == sorted(ids, reverse=True)


# ===========================================================================
# save_rows / get_latest_rows
# ===========================================================================

class TestSaveAndGetRows:
    def test_round_trip_basic(self, tmp_db):
        run_id = tmp_db.start_run("edgx", "cboe")
        rows = [make_row(liq_code="CA", take_rate=-0.47)]
        _finish(tmp_db, run_id, rows)
        retrieved = tmp_db.get_latest_rows("edgx")
        assert len(retrieved) == 1
        assert retrieved[0]["liq_code"] == "CA"
        assert retrieved[0]["take_rate"] == -0.47

    def test_all_rate_fields_stored(self, tmp_db):
        run_id = tmp_db.start_run("edgx", "cboe")
        rows = [make_row(
            make_rate=0.10, take_rate=-0.47,
            auction_init_rate=-0.06, auction_resp_rate=0.50, breakup_rate=-0.05,
        )]
        _finish(tmp_db, run_id, rows)
        retrieved = tmp_db.get_latest_rows("edgx")[0]
        assert retrieved["make_rate"] == 0.10
        assert retrieved["auction_init_rate"] == -0.06
        assert retrieved["auction_resp_rate"] == 0.50
        assert retrieved["breakup_rate"] == -0.05

    def test_footnote_refs_stored_as_json(self, tmp_db):
        run_id = tmp_db.start_run("edgx", "cboe")
        rows = [make_row(footnote_refs=["1", "*"])]
        _finish(tmp_db, run_id, rows)
        retrieved = tmp_db.get_latest_rows("edgx")[0]
        refs = json.loads(retrieved["footnote_refs"])
        assert refs == ["1", "*"]

    def test_confidence_and_reason_stored(self, tmp_db):
        run_id = tmp_db.start_run("edgx", "cboe")
        rows = [make_row(confidence="low", confidence_reason="conflicting footnotes")]
        _finish(tmp_db, run_id, rows)
        retrieved = tmp_db.get_latest_rows("edgx")[0]
        assert retrieved["confidence"] == "low"
        assert retrieved["confidence_reason"] == "conflicting footnotes"

    def test_get_latest_rows_returns_only_latest_run(self, tmp_db):
        # Run 1: 1 row
        run1 = tmp_db.start_run("edgx", "cboe")
        _finish(tmp_db, run1, [make_row(liq_code="CA", take_rate=-0.47)])
        # Run 2: different rate
        run2 = tmp_db.start_run("edgx", "cboe")
        _finish(tmp_db, run2, [make_row(liq_code="CA", take_rate=-0.50)])
        retrieved = tmp_db.get_latest_rows("edgx")
        assert len(retrieved) == 1
        assert retrieved[0]["take_rate"] == -0.50  # from run 2

    def test_get_latest_rows_no_run_returns_empty(self, tmp_db):
        assert tmp_db.get_latest_rows("edgx") == []

    def test_save_empty_rows_is_noop(self, tmp_db):
        run_id = tmp_db.start_run("edgx", "cboe")
        tmp_db.save_rows(run_id, [])
        assert tmp_db.get_latest_rows("edgx") == []

    def test_multiple_exchanges_isolated(self, tmp_db):
        run_edgx = tmp_db.start_run("edgx", "cboe")
        _finish(tmp_db, run_edgx, [make_row(exchange_id="edgx", liq_code="CA")])
        run_bzx = tmp_db.start_run("bzx", "cboe")
        _finish(tmp_db, run_bzx, [make_row(exchange_id="bzx", liq_code="BC")])
        assert len(tmp_db.get_latest_rows("edgx")) == 1
        assert tmp_db.get_latest_rows("edgx")[0]["liq_code"] == "CA"
        assert tmp_db.get_latest_rows("bzx")[0]["liq_code"] == "BC"


# ===========================================================================
# get_all_latest_rows
# ===========================================================================

class TestGetAllLatestRows:
    def test_returns_latest_per_exchange(self, tmp_db):
        for i, (xid, liq) in enumerate([("edgx", "CA"), ("bzx", "BC"), ("c2", "PC")]):
            run = tmp_db.start_run(xid, "cboe")
            _finish(tmp_db, run, [make_row(exchange_id=xid, liq_code=liq)])
        rows = tmp_db.get_all_latest_rows()
        assert len(rows) == 3
        ids = {r["exchange_id"] for r in rows}
        assert ids == {"edgx", "bzx", "c2"}

    def test_multiple_runs_per_exchange_only_latest(self, tmp_db):
        for take in [-0.47, -0.48, -0.50]:
            run = tmp_db.start_run("edgx", "cboe")
            _finish(tmp_db, run, [make_row(take_rate=take)])
        rows = tmp_db.get_all_latest_rows()
        assert len(rows) == 1
        assert rows[0]["take_rate"] == -0.50  # most recent

    def test_empty_db_returns_empty(self, tmp_db):
        assert tmp_db.get_all_latest_rows() == []


# ===========================================================================
# get_previous_rows (used by diff engine)
# ===========================================================================

class TestGetPreviousRows:
    def test_returns_run_before_given_id(self, tmp_db):
        run1 = tmp_db.start_run("edgx", "cboe")
        _finish(tmp_db, run1, [make_row(take_rate=-0.47)])
        run2 = tmp_db.start_run("edgx", "cboe")
        _finish(tmp_db, run2, [make_row(take_rate=-0.50)])
        prev = tmp_db.get_previous_rows("edgx", before_run_id=run2)
        assert len(prev) == 1
        assert prev[0]["take_rate"] == -0.47

    def test_no_previous_run_returns_empty(self, tmp_db):
        run1 = tmp_db.start_run("edgx", "cboe")
        _finish(tmp_db, run1, [make_row()])
        prev = tmp_db.get_previous_rows("edgx", before_run_id=run1)
        assert prev == []


# ===========================================================================
# Footnote storage
# ===========================================================================

class TestFootnoteStorage:
    def test_save_and_retrieve_footnotes(self, tmp_db):
        run_id = tmp_db.start_run("edgx", "cboe")
        fns = [
            make_footnote("1", "Volume threshold applies.", "Page 1"),
            make_footnote("*", "PFOF waiver condition.", "Page 2"),
        ]
        rows = [make_row()]
        result = ExtractionResult(
            exchange_id="edgx", operator="cboe", extracted_at=NOW,
            rows=rows, footnotes=fns,
        )
        tmp_db.finish_run(run_id, result)
        tmp_db.save_rows(run_id, rows)
        tmp_db.save_footnotes(run_id, fns)
        retrieved = tmp_db.get_footnotes("edgx")
        assert len(retrieved) == 2
        refs = {fn["ref"] for fn in retrieved}
        assert refs == {"1", "*"}

    def test_no_footnotes_returns_empty(self, tmp_db):
        run_id = tmp_db.start_run("edgx", "cboe")
        _finish(tmp_db, run_id, [make_row()])
        assert tmp_db.get_footnotes("edgx") == []

    def test_footnote_text_preserved_verbatim(self, tmp_db):
        run_id = tmp_db.start_run("edgx", "cboe")
        long_text = ("Volume threshold: members must maintain ≥ 1,000,000 ADV "
                     "(calculated monthly). Non-qualifying members receive $0.20.")
        fns = [make_footnote("1", long_text, "Page 2")]
        rows = [make_row()]
        result = ExtractionResult(
            exchange_id="edgx", operator="cboe", extracted_at=NOW,
            rows=rows, footnotes=fns,
        )
        tmp_db.finish_run(run_id, result)
        tmp_db.save_rows(run_id, rows)
        tmp_db.save_footnotes(run_id, fns)
        retrieved = tmp_db.get_footnotes("edgx")
        assert retrieved[0]["text"] == long_text


# ===========================================================================
# Flag storage
# ===========================================================================

class TestFlagStorage:
    def test_save_and_retrieve_flags(self, tmp_db):
        run_id = tmp_db.start_run("edgx", "cboe")
        flags = [
            ExtractionFlag("warning", "Page 1", "Ambiguous table layout."),
            ExtractionFlag("error", "Page 3", "Rate cell missing."),
        ]
        rows = [make_row()]
        result = ExtractionResult(
            exchange_id="edgx", operator="cboe", extracted_at=NOW,
            rows=rows, flags=flags,
        )
        tmp_db.finish_run(run_id, result)
        tmp_db.save_rows(run_id, rows)
        tmp_db.save_flags(run_id, flags)
        retrieved = tmp_db.get_flags(exchange_id="edgx")
        assert len(retrieved) == 2
        severities = {f["severity"] for f in retrieved}
        assert severities == {"warning", "error"}

    def test_no_flags_returns_empty(self, tmp_db):
        run_id = tmp_db.start_run("edgx", "cboe")
        _finish(tmp_db, run_id, [make_row()])
        assert tmp_db.get_flags(exchange_id="edgx") == []


# ===========================================================================
# get_review_needed
# ===========================================================================

class TestGetReviewNeeded:
    def test_returns_medium_and_low_only(self, tmp_db):
        run_id = tmp_db.start_run("edgx", "cboe")
        rows = [
            make_row(liq_code="CA", confidence="high"),
            make_row(liq_code="NC", confidence="medium",
                     confidence_reason="volume threshold"),
            make_row(liq_code="ZA", confidence="low",
                     confidence_reason="conflicting footnotes"),
        ]
        _finish(tmp_db, run_id, rows)
        review = tmp_db.get_review_needed()
        assert len(review) == 2
        confs = {r["confidence"] for r in review}
        assert "high" not in confs
        assert confs == {"medium", "low"}

    def test_all_high_returns_empty(self, tmp_db):
        run_id = tmp_db.start_run("edgx", "cboe")
        rows = [make_row(liq_code="CA", confidence="high",
                         confidence_reason=None)]
        _finish(tmp_db, run_id, rows)
        assert tmp_db.get_review_needed() == []


# ===========================================================================
# save_raw_file
# ===========================================================================

class TestSaveRawFile:
    def test_round_trip(self, tmp_db):
        run_id = tmp_db.start_run("edgx", "cboe")
        tmp_db.save_raw_file(
            run_id=run_id,
            exchange_id="edgx",
            fetched_at="2026-06-08T17:00:00+00:00",
            file_path="data/raw/edgx/edgx_test.csv",
            url="https://example.com/fee.csv",
            http_status=200,
            content_type="csv",
        )
        # Verify it was stored by checking run_history (no direct query helper,
        # but save_raw_file should not raise)
        history = tmp_db.get_run_history(limit=1)
        assert history[0]["run_id"] == run_id


# ===========================================================================
# get_footnotes with explicit run_id (line 319+)
# ===========================================================================

class TestGetFootnotesExplicitRunId:
    def test_explicit_run_id_returns_that_runs_footnotes(self, tmp_db):
        from src.extractor.claude import ExtractionResult
        fns1 = [make_footnote("1", "First run footnote.", "p1")]
        fns2 = [make_footnote("2", "Second run footnote.", "p2")]

        run1 = tmp_db.start_run("edgx", "cboe")
        r1 = ExtractionResult(exchange_id="edgx", operator="cboe",
                              extracted_at=NOW, rows=[make_row()], footnotes=fns1)
        tmp_db.finish_run(run1, r1)
        tmp_db.save_rows(run1, [make_row()])
        tmp_db.save_footnotes(run1, fns1)

        run2 = tmp_db.start_run("edgx", "cboe")
        r2 = ExtractionResult(exchange_id="edgx", operator="cboe",
                              extracted_at=NOW, rows=[make_row()], footnotes=fns2)
        tmp_db.finish_run(run2, r2)
        tmp_db.save_rows(run2, [make_row()])
        tmp_db.save_footnotes(run2, fns2)

        # Explicitly request run 1's footnotes (not the latest)
        result = tmp_db.get_footnotes("edgx", run_id=run1)
        assert len(result) == 1
        assert result[0]["ref"] == "1"

    def test_get_footnotes_no_run_exists_returns_empty(self, tmp_db):
        # No run at all for this exchange
        result = tmp_db.get_footnotes("nonexistent_exchange")
        assert result == []


# ===========================================================================
# save_flags empty guard (lines 250-251)
# ===========================================================================

class TestSaveFlagsEmptyGuard:
    def test_save_empty_flags_is_noop(self, tmp_db):
        run_id = tmp_db.start_run("edgx", "cboe")
        tmp_db.save_flags(run_id, [])   # should not raise or write anything
        assert tmp_db.get_flags(exchange_id="edgx") == []

    def test_save_empty_footnotes_is_noop(self, tmp_db):
        run_id = tmp_db.start_run("edgx", "cboe")
        tmp_db.save_footnotes(run_id, [])
        assert tmp_db.get_footnotes("edgx") == []


# ===========================================================================
# get_flags without exchange filter (line 337 — the else branch)
# ===========================================================================

class TestGetFlagsNoFilter:
    def test_get_flags_all_exchanges(self, tmp_db):
        from src.extractor.claude import ExtractionFlag, ExtractionResult
        for xid in ("edgx", "bzx"):
            run_id = tmp_db.start_run(xid, "cboe")
            flag = ExtractionFlag("warning", "p1", f"Issue on {xid}")
            r = ExtractionResult(exchange_id=xid, operator="cboe",
                                 extracted_at=NOW, rows=[make_row(exchange_id=xid)],
                                 flags=[flag])
            tmp_db.finish_run(run_id, r)
            tmp_db.save_rows(run_id, [make_row(exchange_id=xid)])
            tmp_db.save_flags(run_id, [flag])

        # Call without exchange_id — should return flags from both exchanges
        all_flags = tmp_db.get_flags()
        assert len(all_flags) == 2
        exchanges = {f["exchange_id"] for f in all_flags}
        assert exchanges == {"edgx", "bzx"}


# ===========================================================================
# _conn() rollback path (lines 48-50)
# ===========================================================================

class TestConnRollback:
    def test_exception_in_transaction_triggers_rollback(self, tmp_db):
        """Force an exception inside _conn() to exercise the rollback path."""
        import sqlite3
        with pytest.raises(sqlite3.OperationalError):
            with tmp_db._conn() as conn:
                conn.execute("INSERT INTO nonexistent_table VALUES (1)")

        # DB should still be usable after rollback
        run_id = tmp_db.start_run("edgx", "cboe")
        assert run_id > 0


# ===========================================================================
# Schema migration — ALTER TABLE executed (lines 156-160)
# ===========================================================================

class TestSchemaMigration:
    def test_missing_column_is_added(self, tmp_path):
        """Simulate an old-schema DB missing source_page/source_section/footnote_refs.

        We must include `confidence` in the base table because _init_schema creates
        idx_fee_rows_confidence which references that column.  The migration then adds
        the other columns (source_page, source_section, footnote_refs).
        """
        import sqlite3
        from src.persistence.db import Database

        db_path = tmp_path / "migration_test.db"

        # Build a "pre-migration" schema that has the core columns + confidence (needed
        # by _init_schema's index) but is missing source_page, source_section,
        # footnote_refs, and confidence_reason — the columns added by _migrate_schema.
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE run_history (
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

            CREATE TABLE fee_rows (
                row_id              INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id              INTEGER NOT NULL,
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
                confidence          TEXT NOT NULL DEFAULT 'high',
                notes               TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_fee_rows_confidence
                ON fee_rows(confidence);
        """)
        conn.commit()
        conn.close()

        # Database() startup should migrate without raising
        db = Database(db_path=db_path)

        # Verify the migration columns were added
        conn2 = sqlite3.connect(db_path)
        cols = {row[1] for row in conn2.execute("PRAGMA table_info(fee_rows)").fetchall()}
        conn2.close()
        for expected_col in ("source_page", "source_section", "footnote_refs", "confidence_reason"):
            assert expected_col in cols, f"Expected migrated column '{expected_col}' not found"
