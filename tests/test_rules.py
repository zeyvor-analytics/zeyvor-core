"""The rule language: what it accepts, what it refuses, and what it compiles to.

The refusals matter as much as the acceptances here. A rule is executed against
somebody's warehouse, so anything this grammar lets through that it does not
understand becomes either a crash in CI or, far worse, a query that runs and
answers a different question than the one written down.
"""

from __future__ import annotations

import pytest

from zeyvor.contract import schema
from zeyvor.contract.generate import suggest_rules
from zeyvor.contract.models import ColumnContract, TableContract, TableRule
from zeyvor.engines.base import BigQueryDialect, DuckDBDialect
from zeyvor.rules.check import check_rules
from zeyvor.rules.grammar import (
    RuleError,
    compile_rule,
    parse_rule,
    referenced_columns,
    validate_rule,
)
from zeyvor.rules.measure import RuleMeasurement

TYPES = {
    "ordered_at": "timestamp",
    "shipped_at": "timestamp",
    "total": "float",
    "subtotal": "float",
    "discount": "float",
    "status": "text",
    "notes": "text",
    "qty": "integer",
}


def sql(expr: str, dialect=None) -> str:
    return compile_rule(parse_rule(expr), dialect or DuckDBDialect(), TYPES)


# ── what it accepts ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "expr",
    [
        "shipped_at >= ordered_at",
        "discount <= subtotal",
        "abs(total - (subtotal - discount)) <= 0.01",
        "status = 'shipped' implies shipped_at is not null",
        "not (discount > total) and subtotal > 0",
        "length(notes) < 500 or notes is null",
        "qty * subtotal >= 0",
        "status <> 'void'",
        "total >= 0 and total <= 1000000",
    ],
)
def test_the_rules_a_person_would_actually_write_all_parse(expr):
    assert sql(expr)


def test_a_bare_column_name_survives_quoting():
    """Real exports have columns with spaces and punctuation in the name."""
    node = parse_rule('"order total" > 0')
    assert referenced_columns(node) == {"order total"}


def test_an_apostrophe_in_a_value_does_not_end_the_string():
    """`'O''Brien'` is one value, not a syntax error — and definitely not two."""
    assert referenced_columns(parse_rule("status = 'it''s fine'")) == {"status"}
    assert "it''s fine" in sql("status = 'it''s fine'")


# ── what it refuses ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "expr",
    [
        "",
        "   ",
        "shipped_at >=",
        "a b c",
        "(total > 1",
        "total = = 1",
        "total ~ 1",
        "total is not",
        "select * from orders",
        "total > (select max(total) from orders)",
        "1; drop table orders",
    ],
)
def test_nonsense_is_refused_rather_than_guessed_at(expr):
    with pytest.raises(RuleError):
        parse_rule(expr)


def test_an_unknown_function_names_the_ones_that_exist():
    """A rejection that does not say what is allowed just costs another round trip."""
    with pytest.raises(RuleError) as caught:
        parse_rule("coalesce(total, 0) > 1")
    assert "abs" in str(caught.value) and "length" in str(caught.value)


def test_a_mistyped_column_suggests_the_real_one():
    with pytest.raises(RuleError) as caught:
        validate_rule("shiped_at >= ordered_at", list(TYPES))
    assert "shipped_at" in str(caught.value)


# ── what the SQL means ────────────────────────────────────────────────────────


def test_a_number_column_is_compared_as_a_number_not_as_text():
    """The source is read as text, where '9' > '10'. Without the cast every
    numeric rule would quietly answer a different question."""
    assert "TRY_CAST" in sql("qty > 5")
    assert "DOUBLE" in sql("total > 5")


def test_a_text_column_is_not_cast_to_a_number():
    assert "DOUBLE" not in sql("status = 'shipped'")


def test_implies_becomes_or_not_and():
    """`a implies b` must not compile to `a and b` — that would fail every row
    where the premise simply does not apply, which is most of them."""
    compiled = sql("status = 'shipped' implies shipped_at is not null")
    assert "NOT" in compiled and "OR" in compiled


def test_precedence_puts_implies_outermost():
    """`a and b implies c` is `(a and b) implies c`. Getting this backwards
    changes the meaning silently rather than failing to parse."""
    node = parse_rule("total > 0 and subtotal > 0 implies status is not null")
    assert node.op == "implies"
    assert node.left.op == "and"


def test_the_same_rule_compiles_differently_per_engine():
    """A contract is meant to be portable, which only means anything if the
    dialect is chosen at compile time rather than baked into the file."""
    duck = sql("total > 5", DuckDBDialect())
    big = sql("total > 5", BigQueryDialect())
    assert duck != big
    assert "FLOAT64" in big


# ── the contract file ─────────────────────────────────────────────────────────


def _contract_text(rules: str) -> str:
    return f"""
version: 1
tables:
  orders:
    columns:
      ordered_at: {{type: timestamp}}
      shipped_at: {{type: timestamp}}
    rules:
{rules}
"""


def test_a_rule_can_be_written_as_a_bare_string():
    contract = schema.loads(_contract_text("      - shipped_at >= ordered_at\n"))
    assert contract.tables["orders"].rules[0].expr == "shipped_at >= ordered_at"


def test_a_bare_string_stays_a_bare_string_when_written_back():
    """Round-tripping must not inflate a one-line rule into a four-line mapping;
    the contract is reviewed as a diff, and churn is what stops it being read."""
    contract = schema.loads(_contract_text("      - shipped_at >= ordered_at\n"))
    assert "- shipped_at >= ordered_at" in schema.dumps(contract)


def test_a_broken_rule_is_caught_when_the_file_loads_not_an_hour_later_in_ci():
    with pytest.raises(schema.ContractError):
        schema.loads(_contract_text("      - shipped_at >=\n"))


def test_a_rule_naming_a_missing_column_is_refused_at_load():
    with pytest.raises(schema.ContractError):
        schema.loads(_contract_text("      - delivered_at >= ordered_at\n"))


def test_a_misspelled_rule_key_is_refused():
    with pytest.raises(schema.ContractError):
        schema.loads(_contract_text("      - {expr: 'shipped_at is not null', mean: x}\n"))


@pytest.mark.parametrize("rate", ["1.5", "-0.1", "1"])
def test_a_violation_budget_outside_zero_to_one_is_refused(rate):
    with pytest.raises(schema.ContractError):
        schema.loads(
            _contract_text(
                f"      - {{expr: 'shipped_at is not null', max_violation_rate: {rate}}}\n"
            )
        )


# ── turning counts into findings ──────────────────────────────────────────────


def _report(rule: TableRule, judged: int, broken: int, rows: int | None = None):
    contract = schema.loads(_contract_text("      - shipped_at >= ordered_at\n"))
    measurement = RuleMeasurement(
        rule=rule,
        table="orders",
        rows=rows if rows is not None else judged,
        judged=judged,
        broken=broken,
    )
    return check_rules(contract, [measurement])


def test_a_rule_that_holds_says_nothing():
    assert _report(TableRule(expr="shipped_at >= ordered_at"), judged=100, broken=0) == []


def test_one_broken_row_is_enough_by_default():
    """A rule is hand-written, so it is an assertion rather than a guess."""
    found = _report(TableRule(expr="shipped_at >= ordered_at"), judged=100, broken=1)
    assert len(found) == 1
    assert found[0].severity.value == "fail"


def test_a_budget_absorbs_the_trickle_it_was_given_for():
    rule = TableRule(expr="shipped_at >= ordered_at", max_violation_rate=0.02)
    assert _report(rule, judged=1000, broken=15) == []
    assert len(_report(rule, judged=1000, broken=25)) == 1


def test_the_rate_is_measured_against_rows_that_could_be_judged():
    """Counting nulls in the denominator would make a column that is getting
    emptier look like a rule that is getting healthier."""
    found = _report(TableRule(expr="shipped_at >= ordered_at"), judged=10, broken=5, rows=1000)
    assert "50.00%" in found[0].found


def test_a_rule_that_could_not_run_warns_rather_than_failing_silently():
    measurement = RuleMeasurement(
        rule=TableRule(expr="shipped_at >= ordered_at"),
        table="orders",
        error="no such column",
    )
    contract = schema.loads(_contract_text("      - shipped_at >= ordered_at\n"))
    found = check_rules(contract, [measurement])
    assert len(found) == 1
    assert found[0].severity.value == "warn"
    assert "not being checked" in found[0].detail


def test_an_ignored_rule_reports_nothing():
    assert _report(TableRule(expr="shipped_at >= ordered_at", ignore=True), 100, 100) == []


# ── suggestions ───────────────────────────────────────────────────────────────


def _table(**columns: str) -> TableContract:
    return TableContract(
        name="t",
        columns={name: ColumnContract(name=name, type=kind) for name, kind in columns.items()},
    )


def test_a_pair_of_dates_whose_names_imply_an_order_is_suggested():
    assert suggest_rules(_table(created_at="timestamp", shipped_at="timestamp")) == [
        "shipped_at >= created_at"
    ]


def test_dates_with_no_ordering_in_their_names_are_left_alone():
    """Suggesting `b_date >= a_date` because it happened to hold is how a file
    fills with noise nobody reads."""
    assert suggest_rules(_table(a_date="date", b_date="date")) == []


def test_a_word_is_not_matched_inside_another_word():
    """`vendor_date` contains 'end'. Substring matching would suggest it."""
    assert suggest_rules(_table(created_at="timestamp", vendor_date="date")) == []


def test_numbers_are_never_suggested_about():
    """`total = subtotal - discount` is right until somebody adds tax."""
    assert suggest_rules(_table(subtotal="float", total="float", discount="float")) == []
