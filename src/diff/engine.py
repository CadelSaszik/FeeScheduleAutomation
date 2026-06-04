"""Diff engine — compare two sets of fee rows and produce a structured change report."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# Fields that constitute the "key" for matching rows across runs
ROW_KEY_FIELDS = ("exchange_id", "ticker_class", "sec_type", "account_type", "trade_type", "liq_code")

# Fields that are rate values we care about diffing
RATE_FIELDS = ("make_rate", "take_rate", "auction_init_rate", "auction_resp_rate", "breakup_rate")

RATE_LABELS = {
    "make_rate": "Make Rate",
    "take_rate": "Take Rate",
    "auction_init_rate": "Auction Init Rate",
    "auction_resp_rate": "Auction Resp Rate",
    "breakup_rate": "Breakup Rate",
}


@dataclass
class RateChange:
    field: str
    field_label: str
    old_value: Optional[float]
    new_value: Optional[float]

    @property
    def delta(self) -> Optional[float]:
        if self.old_value is None or self.new_value is None:
            return None
        return round(self.new_value - self.old_value, 2)

    def fmt(self) -> str:
        old = _fmt_rate(self.old_value)
        new = _fmt_rate(self.new_value)
        delta = self.delta
        if delta is None:
            return f"{self.field_label}: {old} → {new}"
        sign = "+" if delta > 0 else ""
        return f"{self.field_label}: {old} → {new} ({sign}{delta:+.2f})"


@dataclass
class RowChange:
    key: dict
    change_type: str           # "added" | "removed" | "modified"
    rate_changes: list[RateChange] = field(default_factory=list)
    old_row: Optional[dict] = None
    new_row: Optional[dict] = None

    def key_label(self) -> str:
        parts = []
        if self.key.get("account_type"):
            parts.append(self.key["account_type"])
        if self.key.get("ticker_class"):
            parts.append(self.key["ticker_class"])
        if self.key.get("sec_type"):
            parts.append(self.key["sec_type"])
        if self.key.get("trade_type"):
            parts.append(self.key["trade_type"])
        if self.key.get("liq_code"):
            parts.append(f"liq={self.key['liq_code']}")
        return " / ".join(parts) if parts else str(self.key)


@dataclass
class DiffReport:
    exchange_id: str
    has_changes: bool
    added: list[RowChange] = field(default_factory=list)
    removed: list[RowChange] = field(default_factory=list)
    modified: list[RowChange] = field(default_factory=list)

    @property
    def total_changes(self) -> int:
        return len(self.added) + len(self.removed) + len(self.modified)

    def summary_lines(self) -> list[str]:
        lines = []
        if not self.has_changes:
            lines.append(f"{self.exchange_id}: No changes detected.")
            return lines

        lines.append(
            f"{self.exchange_id}: {self.total_changes} change(s) — "
            f"{len(self.added)} added, {len(self.removed)} removed, "
            f"{len(self.modified)} modified"
        )

        for chg in self.modified:
            for rc in chg.rate_changes:
                lines.append(f"  MOD  {chg.key_label()}: {rc.fmt()}")
        for chg in self.added:
            lines.append(f"  ADD  {chg.key_label()}")
        for chg in self.removed:
            lines.append(f"  DEL  {chg.key_label()}")
        return lines


class DiffEngine:
    def diff(
        self,
        exchange_id: str,
        old_rows: list[dict],
        new_rows: list[dict],
    ) -> DiffReport:
        old_map = _index_rows(old_rows)
        new_map = _index_rows(new_rows)

        added: list[RowChange] = []
        removed: list[RowChange] = []
        modified: list[RowChange] = []

        # Rows in new but not old
        for key, new_row in new_map.items():
            if key not in old_map:
                added.append(
                    RowChange(
                        key=_key_dict(key),
                        change_type="added",
                        new_row=new_row,
                    )
                )
            else:
                old_row = old_map[key]
                rate_changes = _compare_rates(old_row, new_row)
                if rate_changes:
                    modified.append(
                        RowChange(
                            key=_key_dict(key),
                            change_type="modified",
                            rate_changes=rate_changes,
                            old_row=old_row,
                            new_row=new_row,
                        )
                    )

        # Rows in old but not new
        for key, old_row in old_map.items():
            if key not in new_map:
                removed.append(
                    RowChange(
                        key=_key_dict(key),
                        change_type="removed",
                        old_row=old_row,
                    )
                )

        has_changes = bool(added or removed or modified)
        logger.info(
            "[%s] Diff: %d added, %d removed, %d modified",
            exchange_id, len(added), len(removed), len(modified),
        )
        return DiffReport(
            exchange_id=exchange_id,
            has_changes=has_changes,
            added=added,
            removed=removed,
            modified=modified,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row_key(row: dict) -> tuple:
    return tuple(str(row.get(f, "") or "").strip().lower() for f in ROW_KEY_FIELDS)


def _key_dict(key: tuple) -> dict:
    return dict(zip(ROW_KEY_FIELDS, key))


def _index_rows(rows: list[dict]) -> dict[tuple, dict]:
    index: dict[tuple, dict] = {}
    for row in rows:
        k = _row_key(row)
        if k in index:
            logger.debug("Duplicate key in rows: %s — keeping last", k)
        index[k] = row
    return index


def _compare_rates(old: dict, new: dict) -> list[RateChange]:
    changes = []
    for field_name in RATE_FIELDS:
        old_val = _norm(old.get(field_name))
        new_val = _norm(new.get(field_name))
        if old_val != new_val:
            changes.append(
                RateChange(
                    field=field_name,
                    field_label=RATE_LABELS[field_name],
                    old_value=old_val,
                    new_value=new_val,
                )
            )
    return changes


def _norm(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _fmt_rate(value: Optional[float]) -> str:
    if value is None:
        return "—"
    if value >= 0:
        return f"+${value:.2f}"
    return f"-${abs(value):.2f}"
