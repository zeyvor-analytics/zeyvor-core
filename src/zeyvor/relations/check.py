"""Turning relationship measurements into violations.

Three things can go wrong with a foreign key, and they want saying differently.

**Orphans.** Child rows pointing at parents that are not there. The interesting
number is not how many rows but how many distinct keys: one deleted customer
producing four thousand orphan orders is one problem, and four thousand distinct
missing keys is a broken load.

**Fan-out.** The parent's key stopped being unique, so every join through it
multiplies rows. Nothing is missing and nothing errors — every total downstream
is simply too big. This is the failure that survives every conventional test,
which is the whole reason this package exists.

**Uncheckable.** A column named in the relationship is gone, or a table could not
be read. Reported as a warning rather than swallowed: a check that quietly stops
checking is worse than one that fails, because the build stays green and the
contract stays reassuring.
"""

from __future__ import annotations

from ..contract.models import Cardinality, Contract, Severity
from ..contract.violations import DEFAULT_SEVERITY, Violation, ViolationType
from .measure import RelationshipMeasurement


def _severity(contract: Contract, measurement: RelationshipMeasurement, kind: ViolationType):
    """Relationship overrides win, then the child table's, then the contract's."""
    relationship = measurement.relationship
    base = DEFAULT_SEVERITY[kind]
    if relationship.on_violation is not None:
        if relationship.on_violation is Severity.IGNORE:
            return Severity.IGNORE
        return relationship.on_violation
    return contract.resolve_severity(base, contract.table(relationship.from_table))


def check_relationship(
    contract: Contract,
    measurement: RelationshipMeasurement,
    *,
    already_reported: set[tuple[ViolationType, str]] | None = None,
) -> list[Violation]:
    """Findings for one relationship.

    `already_reported` carries the column-level violations from the same run, so
    one cause produces one message. The obvious collision is a parent key
    contracted as `unique: true` that gains duplicates: `uniqueness_lost` fires
    on the column and already says joins will fan out, so repeating it as
    `fk_fanout` would be noise. When nobody declared the column unique, though,
    `fk_fanout` is the only thing that catches it — which is the case worth
    having.
    """
    relationship = measurement.relationship
    if relationship.ignore:
        return []
    already_reported = already_reported or set()

    violations: list[Violation] = []

    if not measurement.measured:
        severity = _severity(contract, measurement, ViolationType.RELATIONSHIP_UNCHECKABLE)
        if severity is not Severity.IGNORE:
            violations.append(
                Violation(
                    type=ViolationType.RELATIONSHIP_UNCHECKABLE,
                    table=relationship.from_table,
                    column=relationship.from_column,
                    severity=severity,
                    expected=f"{relationship.child} references {relationship.parent}",
                    found=measurement.error or "could not be measured",
                    detail=(
                        "This relationship is not being checked. A green build does "
                        "not mean the join is intact."
                    ),
                    remedy=(
                        "Fix the column names in the contract, or remove the "
                        "relationship if it no longer exists."
                    ),
                    evidence={"relationship": relationship.key},
                )
            )
        return violations

    # ── orphans ───────────────────────────────────────────────────────────────
    allowed = relationship.max_orphan_rate
    over_budget = (
        measurement.orphan_rows > 0
        if allowed is None
        else measurement.orphan_rate > allowed + 1e-12
    )

    if over_budget:
        severity = _severity(contract, measurement, ViolationType.FK_ORPHANS)
        if severity is not Severity.IGNORE:
            share = measurement.orphan_rate * 100
            keys = measurement.orphan_keys
            violations.append(
                Violation(
                    type=ViolationType.FK_ORPHANS,
                    table=relationship.from_table,
                    column=relationship.from_column,
                    severity=severity,
                    expected=(
                        f"every value present in {relationship.parent}"
                        if allowed is None
                        else f"at most {allowed * 100:g}% of rows without a match in {relationship.parent}"
                    ),
                    found=(
                        f"{measurement.orphan_rows:,} of {measurement.child_valued:,} rows "
                        f"({share:.1f}%) reference {keys:,} key"
                        f"{'' if keys == 1 else 's'} that {'is' if keys == 1 else 'are'} not there"
                    ),
                    detail=_orphan_story(measurement),
                    remedy=(
                        "Load the parent rows first, or allow it deliberately with max_orphan_rate."
                    ),
                    evidence={
                        "relationship": relationship.key,
                        "orphan_rows": measurement.orphan_rows,
                        "orphan_keys": measurement.orphan_keys,
                        "orphan_rate": round(measurement.orphan_rate, 6),
                        "child_valued": measurement.child_valued,
                    },
                )
            )

    # ── fan-out ───────────────────────────────────────────────────────────────
    # Only meaningful when the contract says one parent per key. A relationship
    # deliberately declared one-to-many would have no expectation to break.
    parent_already_flagged = (
        ViolationType.UNIQUENESS_LOST,
        relationship.parent,
    ) in already_reported

    if (
        relationship.cardinality in (Cardinality.MANY_TO_ONE, Cardinality.ONE_TO_ONE)
        and measurement.parent_duplicates > 0
        and not parent_already_flagged
    ):
        severity = _severity(contract, measurement, ViolationType.FK_FANOUT)
        if severity is not Severity.IGNORE:
            duplicates = measurement.parent_duplicates
            violations.append(
                Violation(
                    type=ViolationType.FK_FANOUT,
                    table=relationship.to_table,
                    column=relationship.to_column,
                    severity=severity,
                    expected=f"one row per value, so {relationship.child} joins to exactly one",
                    found=(
                        f"{measurement.parent_rows:,} rows across "
                        f"{measurement.parent_distinct_keys:,} distinct values "
                        f"— {duplicates:,} extra"
                    ),
                    detail=(
                        "Nothing is missing and nothing errors. Every join through this "
                        "key now returns more rows than it did, so every total built on "
                        "it is too big."
                    ),
                    remedy=(
                        "Deduplicate the parent, or change the relationship's "
                        "cardinality if the extra rows are intended."
                    ),
                    evidence={
                        "relationship": relationship.key,
                        "parent_rows": measurement.parent_rows,
                        "parent_distinct_keys": measurement.parent_distinct_keys,
                        "duplicates": duplicates,
                    },
                )
            )

    return violations


def _orphan_story(measurement: RelationshipMeasurement) -> str:
    """The sentence that tells someone which kind of breakage this is."""
    keys = measurement.orphan_keys
    rows = measurement.orphan_rows
    if keys == 1:
        return (
            "A single missing parent accounts for all of them, which usually means "
            "one row was deleted or has not arrived yet."
        )
    if keys > 1 and rows / keys < 1.5:
        return (
            "Almost every orphan is a different key, which looks less like a late "
            "parent and more like the two sides no longer agree on what a key is."
        )
    return f"{keys:,} distinct parents are missing."


def check_relationships(
    contract: Contract,
    measurements: list[RelationshipMeasurement],
    *,
    existing: list[Violation] | None = None,
) -> list[Violation]:
    """Findings for every relationship, with column-level duplicates suppressed."""
    already_reported = {(violation.type, violation.target) for violation in (existing or [])}
    out: list[Violation] = []
    for measurement in measurements:
        out.extend(check_relationship(contract, measurement, already_reported=already_reported))
    return out


__all__ = ["check_relationship", "check_relationships"]
