"""Cross-table checks: the joins between tables, not the columns within one.

Everything else in Zeyvor asks whether a column still means what it meant. This
asks whether two tables still agree — whether the keys on one side are still
present on the other, and whether the parent's key is still unique enough for a
join through it to return what it used to.

Three modules, in the order they run: `infer` proposes relationships from
profiles at `init`, `measure` counts orphans in the engine at `check`, and
`check` turns those counts into violations.
"""

from __future__ import annotations

from .check import check_relationship, check_relationships
from .infer import infer_relationships
from .measure import RelationshipMeasurement, measure_relationship

__all__ = [
    "RelationshipMeasurement",
    "check_relationship",
    "check_relationships",
    "infer_relationships",
    "measure_relationship",
]
