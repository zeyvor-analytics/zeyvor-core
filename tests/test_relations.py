"""Cross-table checks: foreign keys, orphans and fan-out.

Three layers, tested separately because they fail differently.

`infer` is pure — profiles in, proposed relationships out — so it is tested with
hand-built profiles rather than files. That makes the awkward cases cheap to
write: a coincidentally-unique child column, an approximated distinct count, a
name that looks like a key and is not.

`measure` runs real SQL against real CSVs in DuckDB, because the interesting
questions there are about SQL semantics — does a NULL key count as an orphan,
does `'00123'` match `'123'` — and a fake engine would answer them however I
wrote it.

`check` is pure again: measurements in, violations out.
"""

from __future__ import annotations

import os

import pytest

from zeyvor import DuckDBEngine
from zeyvor.contract import generate_contract
from zeyvor.contract.models import Cardinality, Contract, Relationship, Severity, TableContract
from zeyvor.contract.schema import ContractError, dumps, loads, validate_relationship_targets
from zeyvor.contract.violations import ViolationType
from zeyvor.profile.models import ColumnProfile, InferredType, TableProfile
from zeyvor.relations import (
    RelationshipMeasurement,
    check_relationships,
    infer_relationships,
    measure_relationship,
)
from zeyvor.sources import resolve_source

# ── building profiles by hand ─────────────────────────────────────────────────


def column(
    name: str,
    *,
    distinct: int,
    rows: int,
    nulls: int = 0,
    approx: bool = False,
    type_: InferredType = InferredType.INTEGER,
) -> ColumnProfile:
    return ColumnProfile(
        name=name,
        inferred_type=type_,
        row_count=rows,
        null_count=nulls,
        distinct_count=distinct,
        distinct_is_approx=approx,
    )


def table(name: str, rows: int, *columns: ColumnProfile) -> TableProfile:
    return TableProfile(name=name, row_count=rows, columns=list(columns))


def unique(name: str, rows: int) -> ColumnProfile:
    return column(name, distinct=rows, rows=rows)


def repeating(name: str, rows: int, distinct: int = 3) -> ColumnProfile:
    return column(name, distinct=distinct, rows=rows)


# ── inference: the stem rule ──────────────────────────────────────────────────


def test_stem_rule_finds_the_ordinary_star_schema():
    """`orders.customer_id` → `customers.id`.

    The case the ported TypeScript missed entirely, because it only matched a
    key appearing under the same name in both tables. This is the shape of
    almost every warehouse, so missing it made the rules useless on their own.
    """
    profiles = [
        table("orders", 20, unique("order_id", 20), repeating("customer_id", 20, 8)),
        table("customers", 8, unique("id", 8)),
    ]
    (relationship,) = infer_relationships(profiles)

    assert relationship.from_table == "orders"
    assert relationship.from_column == "customer_id"
    assert relationship.to_table == "customers"
    assert relationship.to_column == "id"
    assert relationship.cardinality is Cardinality.MANY_TO_ONE


@pytest.mark.parametrize(
    "parent_table",
    ["customers", "customer", "dim_customers", "stg_customers"],
)
def test_stem_matching_handles_plurals_and_warehouse_prefixes(parent_table):
    profiles = [
        table("orders", 20, repeating("customer_id", 20, 8)),
        table(parent_table, 8, unique("id", 8)),
    ]
    assert len(infer_relationships(profiles)) == 1


def test_stem_matching_handles_y_to_ies():
    profiles = [
        table("orders", 20, repeating("company_id", 20, 8)),
        table("companies", 8, unique("id", 8)),
    ]
    (relationship,) = infer_relationships(profiles)
    assert relationship.to_table == "companies"


def test_a_table_that_merely_contains_the_stem_is_not_the_parent():
    """`customer_events` is not the dimension `customer_id` points at.

    Substring matching would link a fact table to another fact table and produce
    a relationship that fails on the first run.
    """
    profiles = [
        table("orders", 20, repeating("customer_id", 20, 8)),
        table("customer_events", 40, unique("id", 40)),
    ]
    assert infer_relationships(profiles) == []


# ── inference: the shared-column rule ─────────────────────────────────────────


def test_shared_key_column_uses_the_unique_side_as_parent():
    profiles = [
        table("order_items", 40, repeating("order_id", 40, 10)),
        table("orders", 10, unique("order_id", 10)),
    ]
    (relationship,) = infer_relationships(profiles)
    assert relationship.from_table == "order_items"
    assert relationship.to_table == "orders"


def test_a_unique_child_is_recorded_as_one_to_one():
    profiles = [
        table("order_payments", 10, unique("order_id", 10)),
        table("orders", 10, unique("order_id", 10)),
    ]
    relationships = infer_relationships(profiles)
    assert relationships
    assert all(rel.cardinality is Cardinality.ONE_TO_ONE for rel in relationships)


def test_shared_column_in_only_one_table_is_not_a_relationship():
    profiles = [
        table("orders", 20, repeating("basket_id", 20, 8)),
        table("customers", 8, unique("id", 8)),
    ]
    assert infer_relationships(profiles) == []


# ── inference: refusing to guess ──────────────────────────────────────────────


def test_a_bare_id_is_never_a_foreign_key():
    """Every table has one. Treating it as a reference links everything to everything."""
    profiles = [
        table("orders", 10, unique("id", 10)),
        table("customers", 10, unique("id", 10)),
    ]
    assert infer_relationships(profiles) == []


@pytest.mark.parametrize(
    "name", ["post_code", "country_code", "invoice_no", "status_code", "currency_code"]
)
def test_things_that_look_like_keys_and_are_not(name):
    profiles = [
        table("orders", 20, repeating(name, 20, 5)),
        table("posts", 5, unique("id", 5)),
        table("countries", 5, unique("id", 5)),
    ]
    assert all(rel.from_column != name for rel in infer_relationships(profiles))


def test_uniqueness_on_a_tiny_table_is_a_coincidence_not_a_key():
    """Five distinct values in five rows says nothing. A sixth row could repeat."""
    profiles = [
        table("orders", 5, repeating("customer_id", 5, 5)),
        table("customers", 5, unique("id", 5)),
    ]
    assert infer_relationships(profiles) == []


def test_an_approximate_distinct_count_is_not_evidence_of_a_key():
    """BigQuery's cheap count is fine for reporting and useless for deciding this."""
    profiles = [
        table("orders", 20, repeating("customer_id", 20, 8)),
        table("customers", 8, column("id", distinct=8, rows=8, approx=True)),
    ]
    assert infer_relationships(profiles) == []


def test_a_nullable_column_is_not_a_primary_key():
    profiles = [
        table("orders", 20, repeating("customer_id", 20, 8)),
        table("customers", 8, column("id", distinct=8, rows=8, nulls=2)),
    ]
    assert infer_relationships(profiles) == []


def test_one_table_has_no_relationships_to_find():
    assert infer_relationships([table("orders", 20, unique("order_id", 20))]) == []


def test_inference_is_deterministic():
    """Two runs over the same data must produce the same contract, or every
    `zeyvor init` shows a diff that means nothing."""
    profiles = [
        table("orders", 20, unique("order_id", 20), repeating("customer_id", 20, 8)),
        table("customers", 8, unique("id", 8)),
        table("order_items", 60, repeating("order_id", 60, 20)),
    ]
    first = [rel.key for rel in infer_relationships(profiles)]
    second = [rel.key for rel in infer_relationships(list(reversed(profiles)))]
    assert first == sorted(first)
    assert first == second


def test_no_relationship_points_at_its_own_table():
    profiles = [
        table("orders", 20, unique("order_id", 20), repeating("parent_order_id", 20, 5)),
        table("customers", 8, unique("id", 8)),
    ]
    assert all(rel.from_table != rel.to_table for rel in infer_relationships(profiles))


# ── measurement, against real SQL ─────────────────────────────────────────────


@pytest.fixture
def star(tmp_path):
    """A small star schema on disk, plus a helper that measures against it."""

    def _build(child_rows: str, parent_rows: str):
        child = tmp_path / "orders.csv"
        parent = tmp_path / "customers.csv"
        child.write_text("order_id,customer_id\n" + child_rows, encoding="utf-8")
        parent.write_text("id,name\n" + parent_rows, encoding="utf-8")

        relationship = Relationship(
            from_table="orders",
            from_column="customer_id",
            to_table="customers",
            to_column="id",
        )
        engine = DuckDBEngine()
        try:
            child_resolved = resolve_source(str(child), engine=engine)
            parent_resolved = resolve_source(str(parent), engine=engine)
            return measure_relationship(
                engine, relationship, child_resolved.relation, parent_resolved.relation
            )
        finally:
            engine.close()

    return _build


def test_an_intact_key_reports_no_orphans(star):
    measurement = star("1,1\n2,2\n3,1\n", "1,Ada\n2,Grace\n")
    assert measurement.measured
    assert measurement.orphan_rows == 0
    assert measurement.child_valued == 3
    assert measurement.parent_duplicates == 0


def test_a_missing_parent_is_an_orphan(star):
    measurement = star("1,1\n2,99\n3,1\n", "1,Ada\n2,Grace\n")
    assert measurement.orphan_rows == 1
    assert measurement.orphan_keys == 1
    assert measurement.orphan_rate == pytest.approx(1 / 3)


def test_one_missing_parent_many_orphan_rows(star):
    """The distinct-key count is what separates one deletion from a broken load."""
    measurement = star("1,99\n2,99\n3,99\n4,1\n", "1,Ada\n")
    assert measurement.orphan_rows == 3
    assert measurement.orphan_keys == 1


def test_a_null_key_is_not_an_orphan(star):
    """A nullable foreign key means "no parent", which is a legitimate statement."""
    measurement = star("1,1\n2,\n3,\n", "1,Ada\n")
    assert measurement.child_rows == 3
    assert measurement.child_valued == 1
    assert measurement.orphan_rows == 0


def test_an_empty_string_is_not_a_key(star):
    """Left alone it would join to the parent's own empty string and look fine."""
    measurement = star("1,1\n2,\n", "1,Ada\n,Nobody\n")
    assert measurement.orphan_rows == 0
    assert measurement.child_valued == 1


def test_lost_leading_zeros_show_up_as_orphans(star):
    """`'00123'` does not equal `'123'`, which is the finding rather than a bug.

    The join in the user's own warehouse would miss in exactly the same way.
    """
    measurement = star("1,123\n", "00123,Ada\n")
    assert measurement.orphan_rows == 1


def test_whitespace_is_trimmed_rather_than_reported(star):
    """Padding is not a semantic break, and every database would match these."""
    measurement = star("1, 1 \n", "1,Ada\n")
    assert measurement.orphan_rows == 0


def test_a_duplicated_parent_key_is_counted(star):
    measurement = star("1,1\n", "1,Ada\n1,Ada Lovelace\n2,Grace\n")
    assert measurement.parent_rows == 3
    assert measurement.parent_distinct_keys == 2
    assert measurement.parent_duplicates == 1


def test_a_missing_column_is_reported_not_raised(star, tmp_path):
    """One broken relationship must not abandon the other nineteen."""
    child = tmp_path / "c.csv"
    parent = tmp_path / "p.csv"
    child.write_text("a\n1\n", encoding="utf-8")
    parent.write_text("b\n1\n", encoding="utf-8")

    relationship = Relationship(from_table="c", from_column="nope", to_table="p", to_column="b")
    engine = DuckDBEngine()
    try:
        measurement = measure_relationship(
            engine,
            relationship,
            resolve_source(str(child), engine=engine).relation,
            resolve_source(str(parent), engine=engine).relation,
        )
    finally:
        engine.close()

    assert not measurement.measured
    assert measurement.error


# ── from measurement to violation ─────────────────────────────────────────────


def contract_with(relationship: Relationship, **table_kwargs) -> Contract:
    return Contract(
        tables={
            "orders": TableContract(name="orders", **table_kwargs),
            "customers": TableContract(name="customers"),
        },
        relationships=[relationship],
    )


def a_relationship(**kwargs) -> Relationship:
    return Relationship(
        from_table="orders",
        from_column="customer_id",
        to_table="customers",
        to_column="id",
        **kwargs,
    )


def test_orphans_become_a_failure():
    relationship = a_relationship()
    measurement = RelationshipMeasurement(
        relationship=relationship, child_rows=100, child_valued=100, orphan_rows=3, orphan_keys=2
    )
    (violation,) = check_relationships(contract_with(relationship), [measurement])

    assert violation.type is ViolationType.FK_ORPHANS
    assert violation.severity is Severity.FAIL
    assert violation.target == "orders.customer_id"
    assert violation.evidence["orphan_keys"] == 2


def test_max_orphan_rate_tolerates_what_it_says():
    """A rule that cannot be relaxed is a rule that gets deleted wholesale."""
    relationship = a_relationship(max_orphan_rate=0.05)
    within = RelationshipMeasurement(
        relationship=relationship, child_valued=100, orphan_rows=4, orphan_keys=4
    )
    beyond = RelationshipMeasurement(
        relationship=relationship, child_valued=100, orphan_rows=6, orphan_keys=6
    )
    assert check_relationships(contract_with(relationship), [within]) == []
    assert len(check_relationships(contract_with(relationship), [beyond])) == 1


def test_a_rate_exactly_at_the_limit_passes():
    relationship = a_relationship(max_orphan_rate=0.05)
    measurement = RelationshipMeasurement(
        relationship=relationship, child_valued=100, orphan_rows=5, orphan_keys=5
    )
    assert check_relationships(contract_with(relationship), [measurement]) == []


def test_fanout_is_reported_when_the_parent_key_repeats():
    relationship = a_relationship()
    measurement = RelationshipMeasurement(
        relationship=relationship,
        child_valued=10,
        parent_rows=12,
        parent_distinct_keys=10,
    )
    (violation,) = check_relationships(contract_with(relationship), [measurement])

    assert violation.type is ViolationType.FK_FANOUT
    assert violation.target == "customers.id"
    assert violation.evidence["duplicates"] == 2


def test_fanout_is_suppressed_when_the_column_already_said_so():
    """`uniqueness_lost` already tells the reader joins will fan out.

    Two messages for one cause is the noise the cascade rules exist to prevent.
    """
    from zeyvor.contract.violations import Violation

    relationship = a_relationship()
    measurement = RelationshipMeasurement(
        relationship=relationship, parent_rows=12, parent_distinct_keys=10
    )
    existing = [
        Violation(
            type=ViolationType.UNIQUENESS_LOST,
            table="customers",
            column="id",
            severity=Severity.FAIL,
        )
    ]
    assert check_relationships(contract_with(relationship), [measurement], existing=existing) == []


def test_fanout_still_fires_when_nobody_declared_the_column_unique():
    """The case only this check catches — and the reason it exists."""
    relationship = a_relationship()
    measurement = RelationshipMeasurement(
        relationship=relationship, parent_rows=12, parent_distinct_keys=10
    )
    (violation,) = check_relationships(contract_with(relationship), [measurement], existing=[])
    assert violation.type is ViolationType.FK_FANOUT


def test_an_unmeasurable_relationship_warns_rather_than_passing_quietly():
    relationship = a_relationship()
    measurement = RelationshipMeasurement(relationship=relationship, error="no such column")
    (violation,) = check_relationships(contract_with(relationship), [measurement])

    assert violation.type is ViolationType.RELATIONSHIP_UNCHECKABLE
    assert violation.severity is Severity.WARN
    assert "not being checked" in violation.detail


def test_ignore_silences_a_relationship_entirely():
    relationship = a_relationship(ignore=True)
    measurement = RelationshipMeasurement(
        relationship=relationship, child_valued=10, orphan_rows=10, orphan_keys=10
    )
    assert check_relationships(contract_with(relationship), [measurement]) == []


def test_a_relationship_can_downgrade_its_own_severity():
    relationship = a_relationship(on_violation=Severity.WARN)
    measurement = RelationshipMeasurement(
        relationship=relationship, child_valued=10, orphan_rows=1, orphan_keys=1
    )
    (violation,) = check_relationships(contract_with(relationship), [measurement])
    assert violation.severity is Severity.WARN


def test_warn_only_reaches_relationships_too():
    """An adoption run must not fail the build on a join either."""
    relationship = a_relationship()
    contract = contract_with(relationship)
    contract.defaults.on_violation = Severity.WARN
    measurement = RelationshipMeasurement(
        relationship=relationship, child_valued=10, orphan_rows=1, orphan_keys=1
    )
    (violation,) = check_relationships(contract, [measurement])
    assert violation.severity is Severity.WARN


def test_one_missing_parent_is_described_differently_from_many():
    relationship = a_relationship()
    single = RelationshipMeasurement(
        relationship=relationship, child_valued=100, orphan_rows=40, orphan_keys=1
    )
    scattered = RelationshipMeasurement(
        relationship=relationship, child_valued=100, orphan_rows=40, orphan_keys=39
    )
    (one,) = check_relationships(contract_with(relationship), [single])
    (many,) = check_relationships(contract_with(relationship), [scattered])
    assert "single missing parent" in one.detail
    assert "no longer agree" in many.detail


# ── the contract file ─────────────────────────────────────────────────────────

TWO_TABLES = """\
version: 1
tables:
  orders:
    columns:
      customer_id: {type: integer}
  customers:
    columns:
      id: {type: integer}
"""


def test_a_relationship_round_trips():
    text = (
        TWO_TABLES
        + """\
relationships:
  - means: Every order belongs to a customer.
    from: orders.customer_id
    to: customers.id
    cardinality: many_to_one
    max_orphan_rate: 0.01
"""
    )
    contract = loads(text)
    (relationship,) = contract.relationships
    assert relationship.child == "orders.customer_id"
    assert relationship.parent == "customers.id"
    assert relationship.max_orphan_rate == 0.01
    assert relationship.means

    reloaded = loads(dumps(contract, header=False))
    assert reloaded.relationships == contract.relationships


def test_cardinality_is_always_written_out():
    """A reviewer should not have to know the default to read the clause."""
    contract = loads(
        TWO_TABLES + "relationships:\n  - {from: orders.customer_id, to: customers.id}\n"
    )
    assert "cardinality: many_to_one" in dumps(contract, header=False)


@pytest.mark.parametrize(
    "target", ["orders", "orders.customer_id.extra", ".customer_id", "orders."]
)
def test_a_target_must_be_table_dot_column(target):
    with pytest.raises(ContractError, match="table.column"):
        loads(TWO_TABLES + f"relationships:\n  - {{from: {target}, to: customers.id}}\n")


def test_an_unknown_relationship_key_is_a_typo_not_a_shrug():
    with pytest.raises(ContractError):
        loads(
            TWO_TABLES
            + "relationships:\n  - {from: orders.customer_id, to: customers.id, cardinalty: many_to_one}\n"
        )


def test_an_invented_cardinality_is_refused():
    with pytest.raises(ContractError, match="cardinality"):
        loads(
            TWO_TABLES
            + "relationships:\n  - {from: orders.customer_id, to: customers.id, cardinality: some_to_some}\n"
        )


def test_an_orphan_rate_outside_zero_to_one_is_refused():
    with pytest.raises(ContractError, match="between 0 and 1"):
        loads(
            TWO_TABLES
            + "relationships:\n  - {from: orders.customer_id, to: customers.id, max_orphan_rate: 5}\n"
        )


def test_the_same_edge_twice_is_refused():
    """Which one wins would depend on list order, so neither is allowed to."""
    with pytest.raises(ContractError, match="declared twice"):
        loads(
            TWO_TABLES
            + "relationships:\n"
            + "  - {from: orders.customer_id, to: customers.id}\n"
            + "  - {from: orders.customer_id, to: customers.id}\n"
        )


def test_relationships_must_live_with_the_child_table():
    one_table = """\
version: 1
tables:
  customers:
    columns:
      id: {type: integer}
relationships:
  - from: orders.customer_id
    to: customers.id
"""
    with pytest.raises(ContractError, match="holding the foreign key"):
        loads(one_table)


def test_a_relationship_pointing_at_a_missing_column_is_refused():
    contract = loads(
        TWO_TABLES + "relationships:\n  - {from: orders.customer_id, to: customers.nope}\n"
    )
    with pytest.raises(ContractError, match="does not describe"):
        validate_relationship_targets(contract)


def test_relationships_are_not_lost_by_a_directory_round_trip(tmp_path):
    """A dbt project keeps one file per model; the clause belongs with the child."""
    from zeyvor.contract.schema import dump_directory, load_directory

    contract = loads(
        TWO_TABLES + "relationships:\n  - {from: orders.customer_id, to: customers.id}\n"
    )
    dump_directory(contract, str(tmp_path), header=False)

    assert sorted(os.listdir(tmp_path)) == ["customers.yml", "orders.yml"]
    # The clause is written into the child's file only.
    assert "relationships" in (tmp_path / "orders.yml").read_text(encoding="utf-8")
    assert "relationships" not in (tmp_path / "customers.yml").read_text(encoding="utf-8")

    reloaded = load_directory(str(tmp_path))
    assert [rel.key for rel in reloaded.relationships] == ["orders.customer_id->customers.id"]


def test_a_contract_with_no_relationships_writes_no_key():
    profile = table("orders", 10, unique("order_id", 10))
    text = dumps(generate_contract(profile), header=False)
    assert "relationships" not in text
