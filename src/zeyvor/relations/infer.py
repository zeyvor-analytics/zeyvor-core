"""Working out which columns join to which, from profiles alone.

Ported from the TypeScript that shipped with Zeyvor's retired pipeline builder,
with two deliberate changes.

**No language model.** The original ran rules first and then asked Claude to
find whatever the rules missed. That cannot come here: the rule everywhere else
in this package is that a model may only ever *remove* an assertion, never add
one, because a wrong assertion cries wolf and a tool that cries wolf gets
deleted. A relationship is an assertion — it makes `zeyvor check` run a join and
fail a build — so it has to be earned deterministically or not written at all.

**A second rule, because the first one missed the common case.** The original
only matched a key column appearing under the same name in two tables. That
finds `order_items.order_id → orders.order_id`, and misses
`orders.customer_id → customers.id` — which is the ordinary shape of every star
schema, and precisely why the original needed the model to fill the gap. The
stem rule below closes it: a column named `<stem>_id` looks for a table whose
name is that stem, singular or plural, holding a unique key.

Everything here is conservative on purpose. A relationship that is proposed and
wrong costs a reviewer an argument; one that is missed costs nothing, because
`relationships:` is a hand-editable list and adding one is a two-line diff.
"""

from __future__ import annotations

import re

from ..contract.models import Cardinality, Relationship
from ..profile.models import TableProfile

# An underscore-separated key suffix. A bare "id" is excluded on purpose: it is
# a table's own primary key, not a reference to somebody else's, and treating
# every `id` as a foreign key would link every table to every other. The suffix
# form also avoids words that merely end in the letters — "paid", "valid".
KEY_SUFFIX = re.compile(r"_(id|key|code|fk|no|num)$", re.IGNORECASE)

# Words that look like keys but are almost never foreign keys: a postcode is not
# a reference to a postcodes table.
NOT_A_KEY = frozenset(
    {
        "post_code",
        "postal_code",
        "zip_code",
        "area_code",
        "country_code",
        "currency_code",
        "phone_no",
        "vat_no",
        "invoice_no",
        "serial_no",
        "error_code",
        "status_code",
    }
)

MIN_ROWS_FOR_UNIQUENESS = 8
"""Below this, all-distinct is a coincidence rather than evidence of a key.

Five rows of five different dates are not a primary key, and treating them as
one produces a relationship that fails the moment a sixth row arrives.
"""


def _is_key_name(name: str) -> bool:
    lower = name.lower()
    return bool(KEY_SUFFIX.search(lower)) and lower not in NOT_A_KEY


def _stem(name: str) -> str:
    """`customer_id` → `customer`. Empty when there is nothing left."""
    return KEY_SUFFIX.sub("", name.lower())


def _looks_unique(profile: TableProfile, column: str) -> bool:
    """Does this column behave like a key in this table?

    Distinctness has to be exact to mean anything here. BigQuery's cheap
    approximate count is fine for reporting a cardinality and useless for
    deciding "is this the primary key", so an approximated count is treated as
    not-unique rather than guessed at.
    """
    col = profile.get(column)
    if col is None or profile.row_count < MIN_ROWS_FOR_UNIQUENESS:
        return False
    if col.distinct_is_approx:
        return False
    # Nulls cannot be part of a primary key, so a nullable column is only unique
    # if the distinct values account for every row.
    return col.null_count == 0 and col.distinct_count >= profile.row_count


def _matches_stem(table_name: str, stem: str) -> bool:
    """Would a table with this name plausibly own `<stem>_id`?

    Deliberately tight. Substring matching would make `orders` a candidate owner
    of `order_id` *and* of `reorder_id`, and would match `customer` inside
    `customer_events`, which is not a dimension table.
    """
    if not stem:
        return False
    name = table_name.lower().split(".")[-1]
    # Strip a common warehouse prefix so dim_customers still matches.
    for prefix in ("dim_", "fact_", "stg_", "raw_", "tbl_"):
        if name.startswith(prefix):
            name = name[len(prefix) :]
            break

    candidates = {stem, stem + "s", stem + "es"}
    if stem.endswith("y"):
        candidates.add(stem[:-1] + "ies")
    if stem.endswith("s"):
        candidates.add(stem[:-1])
    return name in candidates


def infer_relationships(profiles: list[TableProfile]) -> list[Relationship]:
    """Propose foreign keys across a set of profiled tables.

    Returns child → parent, de-duplicated, in a stable order so two runs over
    the same data produce the same contract.
    """
    if len(profiles) < 2:
        return []

    by_name = {profile.name: profile for profile in profiles}
    found: dict[str, Relationship] = {}

    for relationship in _shared_column_rule(profiles) + _stem_rule(profiles, by_name):
        # A relationship cannot point at itself, and the first rule to find a
        # given edge wins — the shared-column rule runs first because seeing the
        # same key name on both sides is stronger evidence than a name match.
        if relationship.from_table == relationship.to_table:
            continue
        found.setdefault(relationship.key, relationship)

    return sorted(found.values(), key=lambda rel: rel.key)


def _shared_column_rule(profiles: list[TableProfile]) -> list[Relationship]:
    """A key-shaped column name appearing in two or more tables.

    The table where it is unique is the parent; the others reference it.
    """
    occurrences: dict[str, list[tuple[TableProfile, str, bool]]] = {}
    for profile in profiles:
        for column in profile.columns:
            if not _is_key_name(column.name):
                continue
            occurrences.setdefault(column.name.lower(), []).append(
                (profile, column.name, _looks_unique(profile, column.name))
            )

    out: list[Relationship] = []
    for lowered, seen in occurrences.items():
        if len(seen) < 2:
            continue

        parents = [entry for entry in seen if entry[2]]
        children = [entry for entry in seen if not entry[2]]

        parent: tuple[TableProfile, str, bool] | None = None
        if len(parents) == 1 and children:
            parent = parents[0]
        else:
            # Ambiguous: either nothing is unique, or several things are — which
            # happens on small extracts where the child side is coincidentally
            # all-distinct. Fall back to the name.
            stem = _stem(lowered)
            for entry in seen:
                if _matches_stem(entry[0].name, stem):
                    parent = entry
                    break

        if parent is None:
            continue

        for child_profile, child_column, child_unique in seen:
            if child_profile.name == parent[0].name:
                continue
            out.append(
                Relationship(
                    from_table=child_profile.name,
                    from_column=child_column,
                    to_table=parent[0].name,
                    to_column=parent[1],
                    cardinality=Cardinality.ONE_TO_ONE if child_unique else Cardinality.MANY_TO_ONE,
                )
            )
    return out


def _stem_rule(
    profiles: list[TableProfile], by_name: dict[str, TableProfile]
) -> list[Relationship]:
    """`orders.customer_id` → the `customers` table's own key.

    The case the original algorithm handed to a language model, done with a rule.
    """
    out: list[Relationship] = []
    for profile in profiles:
        for column in profile.columns:
            if not _is_key_name(column.name):
                continue
            stem = _stem(column.name)
            if not stem:
                continue

            for candidate_name, candidate in by_name.items():
                if candidate_name == profile.name or not _matches_stem(candidate_name, stem):
                    continue

                # Which column on the parent is the key? Its own `id` is the
                # usual answer; the fully-qualified name is the other.
                for parent_column in _key_candidates(candidate, stem, column.name):
                    if not _looks_unique(candidate, parent_column):
                        continue
                    out.append(
                        Relationship(
                            from_table=profile.name,
                            from_column=column.name,
                            to_table=candidate_name,
                            to_column=parent_column,
                            cardinality=Cardinality.ONE_TO_ONE
                            if _looks_unique(profile, column.name)
                            else Cardinality.MANY_TO_ONE,
                        )
                    )
                    break
    return out


def _key_candidates(parent: TableProfile, stem: str, child_column: str) -> list[str]:
    """Plausible primary-key column names on the parent, best first."""
    wanted = [child_column.lower(), "id", f"{stem}_id", f"{stem}_key", f"{stem}_code"]
    available = {column.name.lower(): column.name for column in parent.columns}
    ordered: list[str] = []
    for name in wanted:
        actual = available.get(name)
        if actual and actual not in ordered:
            ordered.append(actual)
    return ordered


__all__ = ["infer_relationships"]
