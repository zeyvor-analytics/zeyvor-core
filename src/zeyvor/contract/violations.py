"""Violations, their severities, and how they read.

Messages are templates filled with measured evidence — never model output. A
check runs on every push, so its wording has to be free, instantaneous and
identical between runs. Reproducible text is also what lets the test suite
assert on exact messages, which is how message quality stops regressing.

Every message answers three questions in the same order:

    what was promised · what was found · what to do about it
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .models import Severity


class ViolationType(str, Enum):
    # structural
    COLUMN_MISSING = "column_missing"
    COLUMN_ADDED = "column_added"
    ROW_COUNT_BELOW_MIN = "row_count_below_min"
    TABLE_MISSING = "table_missing"

    # meaning
    TYPE_CHANGED = "type_changed"
    TYPE_CONTAMINATED = "type_contaminated"
    FORMAT_CHANGED = "format_changed"
    EPOCH_SUSPECTED = "epoch_suspected"
    EXCEL_SERIAL_SUSPECTED = "excel_serial_suspected"

    # categories
    NEW_CATEGORY = "new_category"
    CATEGORY_DISAPPEARED = "category_disappeared"
    CATEGORIES_UNVERIFIABLE = "categories_unverifiable"

    # integrity
    NULLABILITY_VIOLATED = "nullability_violated"
    NULL_RATE_EXCEEDED = "null_rate_exceeded"
    UNIQUENESS_LOST = "uniqueness_lost"
    RANGE_EXCEEDED = "range_exceeded"
    UNIT_SHIFT_SUSPECTED = "unit_shift_suspected"
    STALE_DATA = "stale_data"

    # relationships between tables
    FK_ORPHANS = "fk_orphans"
    FK_FANOUT = "fk_fanout"
    RELATIONSHIP_UNCHECKABLE = "relationship_uncheckable"

    # privacy and hygiene
    PII_APPEARED = "pii_appeared"
    NULL_WORDS_APPEARED = "null_words_appeared"
    MOJIBAKE_APPEARED = "mojibake_appeared"
    MIXED_BOOLEAN_ENCODING = "mixed_boolean_encoding"


DEFAULT_SEVERITY: dict[ViolationType, Severity] = {
    ViolationType.COLUMN_MISSING: Severity.FAIL,
    ViolationType.COLUMN_ADDED: Severity.FAIL,
    ViolationType.ROW_COUNT_BELOW_MIN: Severity.FAIL,
    ViolationType.TABLE_MISSING: Severity.FAIL,
    ViolationType.TYPE_CHANGED: Severity.FAIL,
    ViolationType.TYPE_CONTAMINATED: Severity.FAIL,
    ViolationType.FORMAT_CHANGED: Severity.FAIL,
    ViolationType.EPOCH_SUSPECTED: Severity.FAIL,
    ViolationType.EXCEL_SERIAL_SUSPECTED: Severity.FAIL,
    ViolationType.NEW_CATEGORY: Severity.FAIL,
    ViolationType.NULLABILITY_VIOLATED: Severity.FAIL,
    ViolationType.NULL_RATE_EXCEEDED: Severity.FAIL,
    ViolationType.UNIQUENESS_LOST: Severity.FAIL,
    ViolationType.RANGE_EXCEEDED: Severity.FAIL,
    ViolationType.UNIT_SHIFT_SUSPECTED: Severity.FAIL,
    ViolationType.STALE_DATA: Severity.FAIL,
    ViolationType.PII_APPEARED: Severity.FAIL,
    ViolationType.FK_ORPHANS: Severity.FAIL,
    ViolationType.FK_FANOUT: Severity.FAIL,
    # Hygiene and "we could not check this" findings inform rather than block.
    ViolationType.RELATIONSHIP_UNCHECKABLE: Severity.WARN,
    ViolationType.CATEGORY_DISAPPEARED: Severity.WARN,
    ViolationType.CATEGORIES_UNVERIFIABLE: Severity.WARN,
    ViolationType.NULL_WORDS_APPEARED: Severity.WARN,
    ViolationType.MOJIBAKE_APPEARED: Severity.WARN,
    ViolationType.MIXED_BOOLEAN_ENCODING: Severity.WARN,
}

SYMBOL = {Severity.FAIL: "✖", Severity.WARN: "!", Severity.IGNORE: "·"}


@dataclass
class Violation:
    type: ViolationType
    table: str
    severity: Severity
    column: str | None = None

    expected: str = ""
    found: str = ""
    detail: str = ""
    remedy: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def target(self) -> str:
        return f"{self.table}.{self.column}" if self.column else self.table

    def render(self, *, colour: bool = False) -> str:
        head = f"{SYMBOL[self.severity]} {self.target} — {self.type.value}"
        if colour:
            tint = "\033[31m" if self.severity is Severity.FAIL else "\033[33m"
            head = f"{tint}{head}\033[0m"
        lines = [head]
        if self.expected:
            lines.append(f"    Contract: {self.expected}")
        if self.found:
            lines.append(f"    Found:    {self.found}")
        if self.detail:
            lines.append(f"    {self.detail}")
        if self.remedy:
            lines.append(f"    → {self.remedy}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        data = {
            "type": self.type.value,
            "severity": self.severity.value,
            "table": self.table,
            "column": self.column,
            "expected": self.expected,
            "found": self.found,
            "detail": self.detail,
            "remedy": self.remedy,
            "evidence": self.evidence,
        }
        return {k: v for k, v in data.items() if v not in (None, "", {})}


@dataclass
class Report:
    """The outcome of checking profiles against a contract."""

    violations: list[Violation] = field(default_factory=list)
    tables_checked: int = 0
    columns_checked: int = 0
    relationships_checked: int = 0
    checked_at: str = ""
    zeyvor_version: str = ""

    @property
    def failures(self) -> list[Violation]:
        return [v for v in self.violations if v.severity is Severity.FAIL]

    @property
    def warnings(self) -> list[Violation]:
        return [v for v in self.violations if v.severity is Severity.WARN]

    @property
    def ok(self) -> bool:
        return not self.failures

    @property
    def exit_code(self) -> int:
        """Non-zero breaks the build; warnings alone must not."""
        return 1 if self.failures else 0

    def of_type(self, vtype: ViolationType) -> list[Violation]:
        return [v for v in self.violations if v.type is vtype]

    def has(self, vtype: ViolationType, column: str | None = None) -> bool:
        return any(
            v.type is vtype and (column is None or v.column == column) for v in self.violations
        )

    def render(self, *, colour: bool = False) -> str:
        if not self.violations:
            joins = (
                f", and {self.relationships_checked} join"
                f"{'s' if self.relationships_checked != 1 else ''} are intact"
                if self.relationships_checked
                else ""
            )
            return (
                f"✔ {self.columns_checked} columns across {self.tables_checked} "
                f"table{'s' if self.tables_checked != 1 else ''} match the contract{joins}."
            )

        # Group by column so several findings on one column read as one story.
        blocks: list[str] = []
        seen: list[str] = []
        for violation in self.violations:
            if violation.target not in seen:
                seen.append(violation.target)
        for target in seen:
            for violation in self.violations:
                if violation.target == target:
                    blocks.append(violation.render(colour=colour))

        scope = f"{self.columns_checked} columns"
        if self.relationships_checked:
            scope += (
                f" and {self.relationships_checked} relationship"
                f"{'s' if self.relationships_checked != 1 else ''}"
            )
        summary = f"{len(self.failures)} failed, {len(self.warnings)} warned across {scope}"
        return "\n\n".join(blocks) + f"\n\n{summary}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked_at": self.checked_at,
            "zeyvor_version": self.zeyvor_version,
            "tables_checked": self.tables_checked,
            "columns_checked": self.columns_checked,
            "relationships_checked": self.relationships_checked,
            "failed": len(self.failures),
            "warned": len(self.warnings),
            "violations": [v.to_dict() for v in self.violations],
        }


# ── shared phrasing helpers ───────────────────────────────────────────────────


def humanise_count(count: int, total: int | None = None) -> str:
    if total:
        share = count / total
        precision = 1 if share < 0.1 else 0
        return f"{count:,} of {total:,} rows ({share:.{precision}%})"
    return f"{count:,} rows"


def quote_list(values: list[str], limit: int = 5) -> str:
    shown = [f"'{v}'" for v in values[:limit]]
    if len(values) > limit:
        shown.append(f"… (+{len(values) - limit})")
    return ", ".join(shown)


__all__ = [
    "ViolationType",
    "DEFAULT_SEVERITY",
    "Violation",
    "Report",
    "humanise_count",
    "quote_list",
]
