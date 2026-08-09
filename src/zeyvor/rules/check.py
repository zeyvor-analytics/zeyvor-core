"""Turning rule measurements into findings.

Two things can go wrong, and they read differently.

**Broken.** Rows exist that the rule says should not. The number worth leading
with is the share rather than the count, because a rule is a statement about
every row and "nine rows" means something very different in a table of twelve
than in a table of nine million.

**Uncheckable.** The rule could not be evaluated — a column it names has gone,
or the expression no longer parses. Reported as a warning rather than swallowed,
for the same reason an unmeasurable relationship is: a check that quietly stops
checking leaves the build green and the contract reassuring, which is worse
than a red build because nobody goes looking.
"""

from __future__ import annotations

from ..contract.models import Contract, Severity
from ..contract.violations import DEFAULT_SEVERITY, Violation, ViolationType
from .measure import RuleMeasurement


def _severity(contract: Contract, table_name: str, measurement: RuleMeasurement, kind):
    """The rule's own override wins, then the table's, then the contract's."""
    rule = measurement.rule
    if rule.on_violation is not None:
        return rule.on_violation
    return contract.resolve_severity(DEFAULT_SEVERITY[kind], contract.table(table_name))


def check_rules(contract: Contract, measurements: list[RuleMeasurement]) -> list[Violation]:
    """Findings for every measured rule."""
    violations: list[Violation] = []

    for measurement in measurements:
        rule = measurement.rule
        if rule.ignore:
            continue
        table = measurement.table

        if not measurement.measured:
            severity = _severity(contract, table, measurement, ViolationType.RULE_VIOLATED)
            if severity is Severity.IGNORE:
                continue
            violations.append(
                Violation(
                    type=ViolationType.RULE_VIOLATED,
                    table=table,
                    severity=Severity.WARN,
                    expected=rule.expr,
                    found=measurement.error or "could not be measured",
                    detail=(
                        "This rule is not being checked. A green build does not mean it holds."
                    ),
                    remedy=(
                        "Fix the rule in the contract, or remove it if what it "
                        "describes no longer exists."
                    ),
                    evidence={"rule": rule.label, "uncheckable": True},
                )
            )
            continue

        allowed = rule.max_violation_rate
        # The epsilon keeps a rate that is exactly the stated budget from
        # failing on a float representation error alone.
        over_budget = (
            measurement.broken > 0 if allowed is None else measurement.rate > allowed + 1e-12
        )
        if not over_budget:
            continue

        severity = _severity(contract, table, measurement, ViolationType.RULE_VIOLATED)
        if severity is Severity.IGNORE:
            continue

        share = measurement.rate * 100
        skipped = measurement.rows - measurement.judged
        detail = rule.means or ""
        if skipped:
            # Said plainly because the alternative is someone concluding the
            # rule covers rows it never looked at.
            note = (
                f"{skipped:,} row(s) could not be judged, because a value was "
                f"null or would not read as its declared type."
            )
            detail = f"{detail} {note}".strip()

        violations.append(
            Violation(
                type=ViolationType.RULE_VIOLATED,
                table=table,
                severity=severity,
                expected=(
                    rule.expr
                    if allowed is None
                    else f"{rule.expr} (up to {allowed * 100:g}% of rows may break it)"
                ),
                found=(
                    f"{measurement.broken:,} of {measurement.judged:,} rows break it — {share:.2f}%"
                ),
                detail=detail,
                remedy=(
                    "Fix the rows upstream, relax the rule with max_violation_rate, "
                    "or delete the rule if it no longer describes what the table means."
                ),
                evidence={
                    "rule": rule.label,
                    "expr": rule.expr,
                    "broken": measurement.broken,
                    "judged": measurement.judged,
                    "rows": measurement.rows,
                    "rate": round(measurement.rate, 6),
                },
            )
        )

    return violations
