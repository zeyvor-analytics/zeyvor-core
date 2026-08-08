"""The contract data model.

A contract is a human-editable statement of what each column is *supposed* to
mean, committed next to the code that produces the data. It is the artefact that
turns Zeyvor from a tool someone remembers to run into something that runs on
every build — so its readability in a pull request matters as much as its
expressiveness.

Two design rules govern everything here:

**Every absolute has a graded sibling.** ``nullable: false`` breaks on the first
stray null; ``max_null_rate: 0.02`` tolerates reality. A contract language with
only absolutes teaches users to switch it off.

**Nothing is asserted that a profile could not support.** Generation enforces
that (see ``generate.py``); the model simply makes "unspecified" cheap — every
clause defaults to None, meaning unchecked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

CONTRACT_SCHEMA_VERSION = 1

_DURATION = re.compile(r"^\s*(\d+)\s*([mhdw])\s*$", re.IGNORECASE)
_DURATION_SECONDS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(value: str) -> int:
    """`24h` → 86400. Raises ValueError on anything else.

    A deliberately tiny grammar. Accepting ISO-8601 durations or bare integers
    would mean guessing a unit, and a freshness window guessed wrong in either
    direction is worse than one that refused to parse: too long silently stops
    catching the thing it exists for, too short pages somebody at 4am.
    """
    match = _DURATION.match(str(value))
    if not match:
        raise ValueError(f"'{value}' is not a duration. Use a number and a unit: 90m, 24h, 7d, 2w.")
    amount, unit = match.groups()
    seconds = int(amount) * _DURATION_SECONDS[unit.lower()]
    if seconds <= 0:
        raise ValueError("A freshness window must be longer than zero.")
    return seconds


def humanise_duration(value: str) -> str:
    """`24h` → `24 hours`, for a sentence rather than a clause."""
    match = _DURATION.match(str(value))
    if not match:
        return str(value)
    amount, unit = match.groups()
    name = {"m": "minute", "h": "hour", "d": "day", "w": "week"}[unit.lower()]
    return f"{amount} {name}{'' if amount == '1' else 's'}"


class Severity(str, Enum):
    FAIL = "fail"
    """Break the build."""

    WARN = "warn"
    """Report, but exit zero. Also the whole-contract setting for a team's first
    adoption run, where failing CI on day one would get the tool removed."""

    IGNORE = "ignore"
    """Suppress entirely. Used to retire a check without deleting the clause,
    which keeps the intent visible in review."""


@dataclass
class ColumnContract:
    """What one column promises. Every field is optional; None means unchecked."""

    name: str
    means: str | None = None
    """Plain-English purpose. The only field an LLM writes, and the reason a
    reviewer can tell whether the rest of the clause is right."""

    type: str | None = None
    """An ``InferredType`` value: integer, float, boolean, date, timestamp,
    email, url, uuid, json, text."""

    nullable: bool | None = None
    max_null_rate: float | None = None
    unique: bool | None = None

    formats: list[str] = field(default_factory=list)
    """Allowed value shapes, e.g. ``["####-##-##"]``. Shapes rather than
    strftime: they are what the profiler measures, and they survive review by
    someone who does not write code."""

    categories: list[str] | None = None
    categories_closed: bool = False
    """When true, a value outside ``categories`` is a violation. Only ever set
    where the profile captured a *complete* category set — otherwise a contract
    would fail on categories that merely went unrecorded."""

    minimum: Any = None
    maximum: Any = None
    """Numbers, ISO dates, or the tokens ``today``/``now`` resolved at check
    time. An approved envelope, never a snapshot of last run's extremes."""

    fresh_within: str | None = None
    """How recent the newest value has to be — ``24h``, ``7d``, ``90m``.

    `maximum` bounds the future; this bounds the past, and they catch opposite
    failures. A table whose loader stopped last Tuesday holds nothing invalid:
    every row is well formed, correctly typed, complete, and inside every range
    the contract states. It is simply not being added to any more. Nothing else
    in this file can see that, because nothing else asks *when* the newest row
    arrived — which makes a stopped pipeline the most thoroughly silent failure
    there is."""

    no_pii: bool = False
    known_issues: list[str] = field(default_factory=list)
    """Observations already present when the contract was written, and therefore
    not news. Without this, generating a contract from imperfect data would
    produce warnings on the very next run — the fastest way to lose a user."""

    ignore: bool = False
    on_violation: Severity | None = None

    def is_checked(self) -> bool:
        return not self.ignore


@dataclass
class TableContract:
    name: str
    source: str = ""
    profile_fingerprint: str = ""
    """The profile this was generated from — provenance for review, not a
    correctness check. Data legitimately changes shape; that is the point."""

    min_rows: int | None = None
    allow_new_columns: bool = True
    """A new column is news, not usually a failure. Teams that need stricter
    schema control set this false."""

    allow_missing_columns: bool = False
    columns: dict[str, ColumnContract] = field(default_factory=dict)
    on_violation: Severity | None = None

    def column(self, name: str) -> ColumnContract | None:
        return self.columns.get(name)


@dataclass
class Defaults:
    on_violation: Severity = Severity.FAIL


class Cardinality(str, Enum):
    """How many child rows may point at one parent row.

    Recorded because it decides what a broken relationship *does*. A
    ``many_to_one`` whose parent key gains duplicates makes every join built on
    it multiply rows, which is a different failure from an orphan and needs
    saying differently.
    """

    MANY_TO_ONE = "many_to_one"
    ONE_TO_ONE = "one_to_one"


@dataclass
class Relationship:
    """A foreign key: ``from_table.from_column`` points at ``to_table.to_column``.

    The direction is always child → parent, so `from` is the side holding the
    foreign key. That is the only orientation the checks need, and allowing the
    reverse would mean every rule below had to ask which way round it was.
    """

    from_table: str
    from_column: str
    to_table: str
    to_column: str
    cardinality: Cardinality = Cardinality.MANY_TO_ONE

    max_orphan_rate: float | None = None
    """Share of child rows allowed to reference a missing parent. None means
    zero — a foreign key that permits orphans is not really a foreign key. It
    exists because real warehouses have a soft-deleted dimension somewhere, and
    a rule you cannot relax is a rule that gets deleted."""

    means: str | None = None
    known_issues: list[str] = field(default_factory=list)
    ignore: bool = False
    on_violation: Severity | None = None

    @property
    def child(self) -> str:
        return f"{self.from_table}.{self.from_column}"

    @property
    def parent(self) -> str:
        return f"{self.to_table}.{self.to_column}"

    @property
    def key(self) -> str:
        """Stable identity, for de-duplication and for addressing one in the CLI."""
        return f"{self.child}->{self.parent}"

    def __str__(self) -> str:  # pragma: no cover - display only
        return self.key


@dataclass
class Contract:
    version: int = CONTRACT_SCHEMA_VERSION
    generated_by: str = ""
    generated_at: str = ""
    defaults: Defaults = field(default_factory=Defaults)
    tables: dict[str, TableContract] = field(default_factory=dict)
    relationships: list[Relationship] = field(default_factory=list)

    def table(self, name: str) -> TableContract | None:
        return self.tables.get(name)

    def relationships_for(self, table: str) -> list[Relationship]:
        """Every relationship this table takes part in, either end."""
        return [
            rel for rel in self.relationships if rel.from_table == table or rel.to_table == table
        ]

    def resolve_severity(
        self,
        base: Severity,
        table: TableContract | None = None,
        column: ColumnContract | None = None,
    ) -> Severity:
        """Combine a violation's default severity with any overrides.

        A type whose default is WARN stays a warning unless something explicitly
        says otherwise — so raising the contract-wide default to FAIL does not
        turn every hygiene note into a build break. A type that defaults to FAIL
        follows the usual column → table → contract precedence, which is what
        makes a warn-only adoption run possible.
        """
        explicit = None
        if column is not None and column.on_violation is not None:
            explicit = column.on_violation
        elif table is not None and table.on_violation is not None:
            explicit = table.on_violation

        if explicit is Severity.IGNORE:
            return Severity.IGNORE
        if base is Severity.WARN:
            return explicit or Severity.WARN
        return explicit or self.defaults.on_violation


__all__ = [
    "CONTRACT_SCHEMA_VERSION",
    "Cardinality",
    "Relationship",
    "Severity",
    "ColumnContract",
    "TableContract",
    "Defaults",
    "Contract",
]
