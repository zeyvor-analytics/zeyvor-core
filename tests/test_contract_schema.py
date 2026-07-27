"""YAML reading and writing.

The contract is hand-edited in pull requests, so the two things that matter are
that a typo is an error rather than a silent no-op, and that the file is pleasant
to read once written.
"""

from __future__ import annotations

import pytest

from zeyvor.contract import (
    ColumnContract,
    Contract,
    ContractError,
    Severity,
    TableContract,
    dumps,
    loads,
)

MINIMAL = """
version: 1
tables:
  orders:
    columns:
      order_id:
        type: integer
        unique: true
"""

FULL = """
version: 1
generated_by: zeyvor 0.2.0
generated_at: '2026-05-18'
defaults:
  on_violation: warn
tables:
  orders:
    source: orders.csv
    profile_fingerprint: sha256:abc123
    min_rows: 10
    allow_new_columns: false
    allow_missing_columns: true
    on_violation: fail
    columns:
      signup_date:
        means: Calendar date the customer signed up.
        type: date
        formats: ['####-##-##', '##/##/####']
        nullable: false
        unique: false
        min: '2019-01-01'
        max: today
        no_pii: true
        known_issues: [mixed_types]
      status:
        type: text
        categories: [pending, shipped]
        categories_closed: true
        max_null_rate: 0.05
        on_violation: warn
      scratch:
        ignore: true
"""


# ── loading ───────────────────────────────────────────────────────────────────


def test_minimal_contract_loads():
    contract = loads(MINIMAL)
    assert contract.version == 1
    column = contract.tables["orders"].columns["order_id"]
    assert column.type == "integer"
    assert column.unique is True
    # Unspecified means unchecked, not false.
    assert column.nullable is None
    assert column.categories is None


def test_every_clause_loads():
    contract = loads(FULL)
    table = contract.tables["orders"]
    assert contract.defaults.on_violation is Severity.WARN
    assert table.min_rows == 10
    assert table.allow_new_columns is False
    assert table.allow_missing_columns is True
    assert table.on_violation is Severity.FAIL

    date = table.columns["signup_date"]
    assert date.means.startswith("Calendar date")
    assert date.formats == ["####-##-##", "##/##/####"]
    assert date.minimum == "2019-01-01"
    assert date.maximum == "today"
    assert date.no_pii is True
    assert date.known_issues == ["mixed_types"]

    status = table.columns["status"]
    assert status.categories == ["pending", "shipped"]
    assert status.categories_closed is True
    assert status.max_null_rate == 0.05
    assert status.on_violation is Severity.WARN

    assert table.columns["scratch"].ignore is True
    assert not table.columns["scratch"].is_checked()


# ── errors that must not be silent ────────────────────────────────────────────


def test_a_misspelled_key_is_an_error_with_a_line_and_a_suggestion():
    """A silently ignored 'nullible' is worse than a crash: the reviewer
    believes a rule is enforced when it is not."""
    text = """
version: 1
tables:
  orders:
    columns:
      order_id:
        type: integer
        nullible: false
"""
    with pytest.raises(ContractError) as excinfo:
        loads(text)
    message = str(excinfo.value)
    assert "nullible" in message
    assert "line 8" in message
    assert "nullable" in message  # the suggestion


def test_an_unknown_table_key_is_rejected():
    with pytest.raises(ContractError) as excinfo:
        loads("version: 1\ntables:\n  orders:\n    min_row: 5\n    columns: {}\n")
    assert "min_row" in str(excinfo.value)
    assert "min_rows" in str(excinfo.value)


def test_malformed_yaml_names_the_line():
    with pytest.raises(ContractError) as excinfo:
        loads("version: 1\ntables:\n  orders:\n   columns:\n  bad indent: [\n")
    assert "line" in str(excinfo.value).lower()


def test_an_empty_file_is_rejected():
    with pytest.raises(ContractError, match="empty"):
        loads("")


def test_a_contract_with_no_tables_is_rejected():
    with pytest.raises(ContractError, match="no tables"):
        loads("version: 1\ntables: {}\n")


def test_a_future_schema_version_asks_the_user_to_upgrade():
    with pytest.raises(ContractError) as excinfo:
        loads("version: 99\ntables:\n  t:\n    columns: {}\n")
    assert "Upgrade Zeyvor" in str(excinfo.value)


def test_a_bad_severity_lists_the_valid_options():
    with pytest.raises(ContractError) as excinfo:
        loads("version: 1\ntables:\n  t:\n    columns:\n      c:\n        on_violation: explode\n")
    message = str(excinfo.value)
    assert "explode" in message and "fail" in message and "warn" in message


def test_a_non_boolean_where_a_boolean_belongs_is_rejected():
    with pytest.raises(ContractError, match="true or false"):
        loads("version: 1\ntables:\n  t:\n    columns:\n      c:\n        nullable: yes please\n")


def test_a_non_numeric_null_rate_is_rejected():
    with pytest.raises(ContractError, match="must be a number"):
        loads("version: 1\ntables:\n  t:\n    columns:\n      c:\n        max_null_rate: loads\n")


def test_a_non_integer_min_rows_is_rejected():
    with pytest.raises(ContractError, match="must be an integer"):
        loads("version: 1\ntables:\n  t:\n    min_rows: 1.5\n    columns: {}\n")


def test_a_missing_file_points_at_init():
    from zeyvor.contract import load

    with pytest.raises(ContractError) as excinfo:
        load("/definitely/not/here/zeyvor.yml")
    assert "zeyvor init" in str(excinfo.value)


# ── writing ───────────────────────────────────────────────────────────────────


def test_round_trip_preserves_everything():
    original = loads(FULL)
    restored = loads(dumps(original))

    assert restored.defaults.on_violation is original.defaults.on_violation
    for name, table in original.tables.items():
        copy = restored.tables[name]
        assert copy.min_rows == table.min_rows
        assert copy.allow_new_columns == table.allow_new_columns
        assert copy.allow_missing_columns == table.allow_missing_columns
        for column_name, column in table.columns.items():
            other = copy.columns[column_name]
            assert other.means == column.means
            assert other.type == column.type
            assert other.formats == column.formats
            assert other.categories == column.categories
            assert other.categories_closed == column.categories_closed
            assert other.minimum == column.minimum
            assert other.maximum == column.maximum
            assert other.nullable == column.nullable
            assert other.max_null_rate == column.max_null_rate
            assert other.no_pii == column.no_pii
            assert other.known_issues == column.known_issues
            assert other.ignore == column.ignore
            assert other.on_violation == column.on_violation


def test_output_is_readable():
    contract = Contract(
        tables={
            "orders": TableContract(
                name="orders",
                columns={
                    "signup_date": ColumnContract(
                        name="signup_date",
                        means="Calendar date the customer signed up, in the shop's timezone.",
                        type="date",
                        formats=["####-##-##"],
                        nullable=False,
                    )
                },
            )
        }
    )
    text = dumps(contract)

    assert text.startswith("# Zeyvor data contract")
    # Short lists stay inline rather than exploding over four lines.
    assert "formats: ['####-##-##']" in text
    # A sentence is never wrapped mid-word.
    assert "shop's timezone." in text
    # Key order puts the human-readable part first.
    assert text.index("means:") < text.index("type:") < text.index("nullable:")


def test_defaults_are_omitted_rather_than_restated():
    """Noise costs review attention, so only deviations are written."""
    contract = Contract(
        tables={"t": TableContract(name="t", columns={"c": ColumnContract(name="c", type="text")})}
    )
    text = dumps(contract, header=False)

    assert "defaults" not in text          # fail is the default
    assert "allow_new_columns" not in text  # true is the default
    assert "ignore" not in text
    assert "no_pii" not in text


def test_dump_and_load_from_disk(tmp_path):
    from zeyvor.contract import dump, load

    contract = loads(FULL)
    path = tmp_path / "zeyvor.yml"
    dump(contract, str(path))

    assert load(str(path)).tables["orders"].columns["signup_date"].maximum == "today"
