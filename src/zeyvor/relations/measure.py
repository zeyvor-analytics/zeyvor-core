"""Measuring a foreign key without pulling rows.

This is the first thing in Zeyvor that has to look at two tables at once, and
that changes the shape of a measurement: a profile is per-table and a join is
not. What has not changed is that the counting happens in the engine. One query
per relationship returns four numbers, and no key ever comes back.

The join compares both sides **as text**, which is a decision worth explaining
because it looks sloppy and is not:

- Profiling already reads everything as text, so a key that the source declares
  as BIGINT on one side and VARCHAR on the other still compares.
- `'00123'` does not equal `'123'`, so a column that lost its leading zeros
  reports orphans instead of silently matching. That is the finding, not a bug —
  the join in the user's own warehouse would fail exactly the same way.
- Whitespace is trimmed, because `'123 '` matching `'123'` is what every
  database would do on a real key comparison, and reporting it as an orphan
  would be a false alarm about a padding difference.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..contract.models import Relationship
from ..engines.base import Engine, EngineError, Relation


@dataclass(frozen=True)
class RelationshipMeasurement:
    """What one query found out about one foreign key."""

    relationship: Relationship

    child_rows: int = 0
    child_valued: int = 0
    """Child rows where the key is present. Nulls are not orphans — a nullable
    foreign key means "no parent", which is a legitimate thing to say."""

    orphan_rows: int = 0
    orphan_keys: int = 0
    """Distinct missing keys. One deleted parent producing forty thousand orphan
    rows is a different story from forty thousand separate mistakes, and the
    difference decides what somebody does next."""

    parent_rows: int = 0
    parent_distinct_keys: int = 0

    error: str | None = None
    """Set when the relationship could not be measured at all — a missing column
    or an unreadable table. Reported rather than raised: one broken relationship
    should not abandon the other nineteen."""

    @property
    def orphan_rate(self) -> float:
        return self.orphan_rows / self.child_valued if self.child_valued else 0.0

    @property
    def parent_duplicates(self) -> int:
        """Parent rows beyond one per key. Anything above zero fans out a join."""
        return max(self.parent_rows - self.parent_distinct_keys, 0)

    @property
    def measured(self) -> bool:
        return self.error is None


def _key_expr(dialect, alias: str, column: str) -> str:
    """The comparable form of a key: trimmed text, with empty treated as absent."""
    quoted = f"{alias}.{dialect.quote_ident(column)}"
    trimmed = dialect.trim(dialect.cast(quoted, dialect.string_type))
    # An empty string is not a key. Left alone it would join to any other empty
    # string and report a match that means nothing.
    return f"NULLIF({trimmed}, '')"


def measure_relationship(
    engine: Engine,
    relationship: Relationship,
    child: Relation,
    parent: Relation,
) -> RelationshipMeasurement:
    """Count orphans and parent duplicates in a single query.

    A LEFT JOIN rather than NOT IN: `NOT IN` against a subquery containing a
    single NULL returns no rows at all on most engines, which would report every
    foreign key as perfect. That failure mode is silent, which makes it the worst
    kind.
    """
    dialect = engine.dialect
    child_key = _key_expr(dialect, "c", relationship.from_column)
    parent_key = _key_expr(dialect, "p", relationship.to_column)

    sql = f"""
SELECT
  COUNT(*) AS child_rows,
  {dialect.sum_case(f"{child_key} IS NOT NULL")} AS child_valued,
  {dialect.sum_case(f"{child_key} IS NOT NULL AND {parent_key} IS NULL")} AS orphan_rows,
  {dialect.count_distinct(f"CASE WHEN {child_key} IS NOT NULL AND {parent_key} IS NULL THEN {child_key} END")} AS orphan_keys
FROM {child.sql} AS c
LEFT JOIN {parent.sql} AS p ON {child_key} = {parent_key}
""".strip()

    try:
        row = engine.execute_one(sql)
    except EngineError as exc:
        return RelationshipMeasurement(relationship=relationship, error=_short(exc))

    # The parent side is counted separately. Doing it in the joined query would
    # multiply parent rows by their children and give a meaningless total.
    parent_sql = f"""
SELECT COUNT(*) AS parent_rows,
       {dialect.count_distinct(_key_expr(dialect, "p", relationship.to_column))} AS parent_keys
FROM {parent.sql} AS p
""".strip()

    try:
        parent_row = engine.execute_one(parent_sql)
    except EngineError as exc:
        return RelationshipMeasurement(relationship=relationship, error=_short(exc))

    return RelationshipMeasurement(
        relationship=relationship,
        child_rows=_int(row[0]),
        child_valued=_int(row[1]),
        orphan_rows=_int(row[2]),
        orphan_keys=_int(row[3]),
        parent_rows=_int(parent_row[0]),
        parent_distinct_keys=_int(parent_row[1]),
    )


def _int(value) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _short(exc: Exception, limit: int = 140) -> str:
    text = " ".join(str(exc).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


__all__ = ["RelationshipMeasurement", "measure_relationship"]
