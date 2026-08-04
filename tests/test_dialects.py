"""Dialect coverage.

The warehouse adapters cannot be exercised offline, but the SQL they *generate*
can be. These tests prove the dialect layer is complete for every supported
engine — that adding a new warehouse does not require touching the profiler,
and that no dialect silently falls back to DuckDB syntax it does not support.
"""

from __future__ import annotations

import pytest

from zeyvor.engines.base import BigQueryDialect, DuckDBDialect, PostgresDialect, Relation
from zeyvor.profile import sql as sqlgen
from zeyvor.profile.patterns import ALL_PATTERNS

DIALECTS = [DuckDBDialect(), PostgresDialect(), BigQueryDialect()]
RELATION = Relation(sql='"orders"', name="orders", source_uri="test")
COLUMNS = ["order_id", "signup date", 'weird"name']


@pytest.mark.parametrize("dialect", DIALECTS, ids=lambda d: d.name)
def test_scalar_stats_sql_generates_for_every_dialect(dialect):
    statement, layouts = sqlgen.scalar_stats_sql(dialect, RELATION, COLUMNS, patterns=ALL_PATTERNS)
    assert statement.startswith("SELECT")
    assert f"FROM {RELATION.sql}" in statement
    assert len(layouts) == len(COLUMNS)
    # Every column contributes the same metric set, so results map back by order.
    assert len({tuple(layout.metrics) for layout in layouts}) == 1
    assert statement.count("AS c0__") == len(layouts[0].metrics)


@pytest.mark.parametrize("dialect", DIALECTS, ids=lambda d: d.name)
def test_every_pattern_survives_dialect_rendering(dialect):
    statement, _ = sqlgen.scalar_stats_sql(dialect, RELATION, ["c"], patterns=ALL_PATTERNS)
    for pattern in ALL_PATTERNS:
        assert f"pat_{pattern.key}" in statement


@pytest.mark.parametrize("dialect", DIALECTS, ids=lambda d: d.name)
def test_shape_and_enum_sql_generate(dialect):
    shapes = sqlgen.shape_sql(dialect, RELATION, [(0, "a"), (1, "b")], limit=5)
    assert shapes.count("UNION ALL") == 1
    assert "LIMIT 5" in shapes

    enums = sqlgen.enum_sql(dialect, RELATION, [(0, "a")], limit=32)
    assert "GROUP BY" in enums and "LIMIT 32" in enums


@pytest.mark.parametrize("dialect", DIALECTS, ids=lambda d: d.name)
def test_identifiers_with_quotes_are_escaped(dialect):
    quoted = dialect.quote_ident('weird"name')
    assert quoted.startswith(("`", '"')) and quoted.endswith(("`", '"'))
    statement, _ = sqlgen.scalar_stats_sql(
        dialect, RELATION, ['weird"name'], patterns=ALL_PATTERNS[:1]
    )
    assert quoted in statement


@pytest.mark.parametrize("dialect", DIALECTS, ids=lambda d: d.name)
def test_literals_escape_single_quotes(dialect):
    assert dialect.quote_literal("O'Brien") in ("'O''Brien'", "'O\\'Brien'")


# ── dialect-specific syntax ───────────────────────────────────────────────────


def test_bigquery_uses_safe_cast_backticks_and_raw_regex():
    dialect = BigQueryDialect()
    assert dialect.try_cast("x", "INT64").startswith("SAFE_CAST")
    assert dialect.quote_ident("col") == "`col`"
    # Backslashes in regexes must not be re-interpreted by the string literal.
    assert dialect.quote_regex(r"^[0-9]\.[0-9]$").startswith("r'")
    assert dialect.regex_match("x", "^a$").startswith("REGEXP_CONTAINS")
    assert "APPROX_QUANTILES" in dialect.quantile("x", 0.5)
    assert dialect.exact_distinct_default is False


def test_duckdb_and_postgres_share_execution_but_report_differently():
    """Postgres is reached through DuckDB, so the SQL matches but the label does not."""
    duck, postgres = DuckDBDialect(), PostgresDialect()
    assert duck.regex_match("x", "^a$") == postgres.regex_match("x", "^a$")
    assert duck.name != postgres.name


@pytest.mark.parametrize("dialect", DIALECTS, ids=lambda d: d.name)
def test_no_dialect_leaves_a_placeholder_unimplemented(dialect):
    """Every construct the profiler needs must render to real SQL."""
    for expression in (
        dialect.try_cast("x", dialect.int_type),
        dialect.as_text("x"),
        dialect.length("x"),
        dialect.trim("x"),
        dialect.lower("x"),
        dialect.upper("x"),
        dialect.substr("x", 1, 10),
        dialect.regex_match("x", "^a$"),
        dialect.regex_replace_all("x", "[0-9]", "#"),
        dialect.stddev("x"),
        dialect.quantile("x", 0.5),
        dialect.count_distinct("x"),
        dialect.approx_count_distinct("x"),
        dialect.current_timestamp(),
        dialect.timestamp_literal("1900-01-01 00:00:00"),
        dialect.floor("x"),
        dialect.sum_case("x IS NULL"),
    ):
        assert isinstance(expression, str) and expression.strip()
        assert "None" not in expression
        assert "..." not in expression
