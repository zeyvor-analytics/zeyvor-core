"""Counting the rows that break a rule, without pulling any of them back.

Every rule over one table becomes one column of a single SELECT, so a table
with twenty rules costs one query rather than twenty. The shape is the same as
every other measurement here: aggregates run where the data lives and only
numbers come home.

What comes back per rule is two counts — how many rows could be *judged*, and
how many of those broke it. The first matters because a rule over a null column
is unknown rather than broken (see `grammar`), and a rate computed against all
rows would quietly shrink as nulls grew, turning a worsening column into an
improving number.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..contract.models import TableRule
from ..engines.base import Engine, EngineError, Relation
from .grammar import RuleError, compile_rule, parse_rule


@dataclass(frozen=True)
class RuleMeasurement:
    """What one query column found out about one rule."""

    rule: TableRule
    table: str

    rows: int = 0
    judged: int = 0
    """Rows where the rule evaluated to true or false. Rows where it evaluated
    to NULL — a null operand, or a value that would not cast — are excluded."""

    broken: int = 0

    error: str | None = None
    """Set when this rule could not be measured. Reported rather than raised:
    one unparseable rule should not abandon the other nineteen, and a rule that
    silently stopped being checked is the failure this tool exists to prevent."""

    @property
    def rate(self) -> float:
        return self.broken / self.judged if self.judged else 0.0

    @property
    def measured(self) -> bool:
        return self.error is None


def _measure_one(
    engine: Engine,
    relation: Relation,
    table: str,
    rule: TableRule,
    types: dict[str, str | None],
) -> RuleMeasurement:
    """One rule, one query. The fallback when a batch could not run."""
    try:
        sql = compile_rule(parse_rule(rule.expr), engine.dialect, types)
        query = (
            f"SELECT COUNT(*), {engine.dialect.sum_case(f'({sql}) IS NOT NULL')}, "
            f"{engine.dialect.sum_case(f'NOT ({sql})')}\nFROM {relation.sql}"
        )
        row = engine.execute(query)[0]
    except (RuleError, EngineError, IndexError) as exc:
        return RuleMeasurement(rule=rule, table=table, error=str(exc))
    return RuleMeasurement(
        rule=rule,
        table=table,
        rows=int(row[0] or 0),
        judged=int(row[1] or 0),
        broken=int(row[2] or 0),
    )


def measure_rules(
    engine: Engine,
    relation: Relation,
    table: str,
    rules: list[TableRule],
    types: dict[str, str | None],
) -> list[RuleMeasurement]:
    """Measure every rule on one table in a single query."""
    active = [rule for rule in rules if not rule.ignore]
    if not active:
        return []

    dialect = engine.dialect
    selects: list[str] = ["COUNT(*)"]
    usable: list[TableRule] = []
    failures: list[RuleMeasurement] = []

    for rule in active:
        try:
            sql = compile_rule(parse_rule(rule.expr), dialect, types)
        except RuleError as exc:
            # Contract loading already validates this, so reaching here means
            # the contract was hand-edited into an invalid state after loading,
            # or a column vanished. Either way it is a finding, not a crash.
            failures.append(RuleMeasurement(rule=rule, table=table, error=str(exc)))
            continue
        usable.append(rule)
        # Judged and broken, in that order, per rule. NULL is neither.
        selects.append(dialect.sum_case(f"({sql}) IS NOT NULL"))
        selects.append(dialect.sum_case(f"NOT ({sql})"))

    if not usable:
        return failures

    query = "SELECT\n  " + ",\n  ".join(selects) + f"\nFROM {relation.sql}"
    try:
        row = engine.execute(query)[0]
    except (EngineError, IndexError):
        # One rule naming a column that has since been dropped fails the whole
        # batch, and blaming twenty rules for one rule's mistake is worse than
        # useless — it points every reader at the wrong line. Retry one at a
        # time so each verdict is its own. Only on this path, so the cost is
        # paid by the broken case rather than the normal one.
        return failures + [_measure_one(engine, relation, table, rule, types) for rule in usable]

    rows = int(row[0] or 0)
    measurements: list[RuleMeasurement] = []
    for index, rule in enumerate(usable):
        judged = int(row[1 + index * 2] or 0)
        broken = int(row[2 + index * 2] or 0)
        measurements.append(
            RuleMeasurement(rule=rule, table=table, rows=rows, judged=judged, broken=broken)
        )
    return failures + measurements
