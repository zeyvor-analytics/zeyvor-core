"""Execution engines and SQL dialects.

Zeyvor never pulls rows into Python to profile them. Every measurement is a SQL
aggregate that runs *where the data already lives* — DuckDB locally for files,
or the warehouse itself for Snowflake/BigQuery. An Engine is therefore a very
thin thing: it executes SQL and lists columns. Everything else is SQL
generation, which is the Dialect's job.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Sequence


@dataclass(frozen=True)
class Relation:
    """A SQL table expression that can be dropped straight into a FROM clause.

    `sql` is the expression itself (``read_csv_auto('/tmp/a.csv')`` or
    ``"public"."orders"``), `name` is a display/table name, and `source_uri` is
    whatever the user originally typed.

    `typed_sql` is an optional second expression over the same data with the
    reader's own type detection left switched on. All measurement happens on
    `sql` (text-safe), while `typed_sql` exists purely to report what the source
    *claims* the types are — and a mismatch between the two is itself a finding.
    """

    sql: str
    name: str
    source_uri: str
    typed_sql: str | None = None

    @property
    def declared_sql(self) -> str:
        return self.typed_sql or self.sql


# ── Dialects ──────────────────────────────────────────────────────────────────


class Dialect:
    """Emits the handful of SQL constructs the profiler needs.

    Defaults target DuckDB. Subclasses override only what differs, which keeps
    the surface area of "add a new warehouse" genuinely small.
    """

    name = "duckdb"

    string_type = "VARCHAR"
    int_type = "BIGINT"
    float_type = "DOUBLE"
    date_type = "DATE"
    timestamp_type = "TIMESTAMP"
    bool_type = "BOOLEAN"

    # Some engines (BigQuery) only offer approximate COUNT(DISTINCT) cheaply.
    exact_distinct_default = True

    # ── identifiers and literals ──────────────────────────────────────────────

    def quote_ident(self, name: str) -> str:
        return '"' + str(name).replace('"', '""') + '"'

    def quote_literal(self, value: str) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    def quote_regex(self, pattern: str) -> str:
        """Regex literals need raw-string handling on some engines.

        DuckDB and Snowflake treat backslashes in single-quoted strings
        literally, so a normal literal is correct here.
        """
        return self.quote_literal(pattern)

    # ── casting ───────────────────────────────────────────────────────────────

    def try_cast(self, expr: str, sql_type: str) -> str:
        return f"TRY_CAST({expr} AS {sql_type})"

    def cast(self, expr: str, sql_type: str) -> str:
        return f"CAST({expr} AS {sql_type})"

    def as_text(self, expr: str) -> str:
        """Everything is profiled through its text representation.

        This is what makes the profiler type-agnostic: a BIGINT column and a
        VARCHAR column holding digits are measured with identical SQL, and the
        *inferred* type comes from cast probes rather than the declared type.
        """
        return self.cast(expr, self.string_type)

    # ── functions ─────────────────────────────────────────────────────────────

    def length(self, expr: str) -> str:
        return f"LENGTH({expr})"

    def trim(self, expr: str) -> str:
        return f"TRIM({expr})"

    def lower(self, expr: str) -> str:
        return f"LOWER({expr})"

    def upper(self, expr: str) -> str:
        return f"UPPER({expr})"

    def substr(self, expr: str, start: int, length: int) -> str:
        return f"SUBSTRING({expr}, {start}, {length})"

    def regex_match(self, expr: str, pattern: str) -> str:
        return f"regexp_matches({expr}, {self.quote_regex(pattern)})"

    def regex_replace_all(self, expr: str, pattern: str, replacement: str) -> str:
        return (
            f"regexp_replace({expr}, {self.quote_regex(pattern)}, "
            f"{self.quote_literal(replacement)}, 'g')"
        )

    def stddev(self, expr: str) -> str:
        return f"STDDEV_SAMP({expr})"

    def quantile(self, expr: str, q: float) -> str:
        return f"quantile_cont({expr}, {q})"

    def approx_count_distinct(self, expr: str) -> str:
        return f"APPROX_COUNT_DISTINCT({expr})"

    def count_distinct(self, expr: str) -> str:
        return f"COUNT(DISTINCT {expr})"

    def current_timestamp(self) -> str:
        return "CURRENT_TIMESTAMP"

    def timestamp_literal(self, iso: str) -> str:
        return f"TIMESTAMP {self.quote_literal(iso)}"

    def floor(self, expr: str) -> str:
        return f"FLOOR({expr})"

    # ── helpers ───────────────────────────────────────────────────────────────

    def sum_case(self, condition: str) -> str:
        """SUM of a boolean predicate, NULL-safe and always an integer."""
        return f"COALESCE(SUM(CASE WHEN {condition} THEN 1 ELSE 0 END), 0)"


class DuckDBDialect(Dialect):
    name = "duckdb"


class PostgresDialect(Dialect):
    """Postgres reached through DuckDB's `postgres` extension.

    Queries still execute inside DuckDB (which pushes down what it can), so the
    dialect is DuckDB's. Kept as a distinct class so the profile records the
    real upstream system.
    """

    name = "postgres"


class SnowflakeDialect(Dialect):
    name = "snowflake"
    int_type = "NUMBER(38,0)"
    float_type = "FLOAT"

    def regex_match(self, expr: str, pattern: str) -> str:
        return f"RLIKE({expr}, {self.quote_regex(pattern)})"

    def regex_replace_all(self, expr: str, pattern: str, replacement: str) -> str:
        # Snowflake REGEXP_REPLACE replaces all occurrences by default.
        return (
            f"REGEXP_REPLACE({expr}, {self.quote_regex(pattern)}, "
            f"{self.quote_literal(replacement)})"
        )

    def quantile(self, expr: str, q: float) -> str:
        return f"PERCENTILE_CONT({q}) WITHIN GROUP (ORDER BY {expr})"

    def approx_count_distinct(self, expr: str) -> str:
        return f"APPROX_COUNT_DISTINCT({expr})"


class BigQueryDialect(Dialect):
    name = "bigquery"
    string_type = "STRING"
    int_type = "INT64"
    float_type = "FLOAT64"
    bool_type = "BOOL"
    exact_distinct_default = False

    def quote_ident(self, name: str) -> str:
        return "`" + str(name).replace("`", "\\`") + "`"

    def quote_regex(self, pattern: str) -> str:
        # BigQuery interprets backslashes in normal strings, so regexes must be
        # raw literals or every `\d` would need doubling.
        return "r'" + str(pattern).replace("'", "\\'") + "'"

    def try_cast(self, expr: str, sql_type: str) -> str:
        return f"SAFE_CAST({expr} AS {sql_type})"

    def regex_match(self, expr: str, pattern: str) -> str:
        return f"REGEXP_CONTAINS({expr}, {self.quote_regex(pattern)})"

    def regex_replace_all(self, expr: str, pattern: str, replacement: str) -> str:
        return (
            f"REGEXP_REPLACE({expr}, {self.quote_regex(pattern)}, "
            f"{self.quote_literal(replacement)})"
        )

    def quantile(self, expr: str, q: float) -> str:
        offset = int(round(q * 100))
        return f"APPROX_QUANTILES({expr}, 100)[OFFSET({offset})]"

    def approx_count_distinct(self, expr: str) -> str:
        return f"APPROX_COUNT_DISTINCT({expr})"

    def timestamp_literal(self, iso: str) -> str:
        return f"TIMESTAMP {self.quote_literal(iso)}"


# ── Engines ───────────────────────────────────────────────────────────────────


class Engine(ABC):
    """Executes SQL somewhere. Counts its own queries so we can prove pushdown."""

    dialect: Dialect

    def __init__(self) -> None:
        self.query_count = 0
        self.last_sql: str | None = None

    @abstractmethod
    def _execute(self, sql: str) -> list[tuple[Any, ...]]:
        ...

    def execute(self, sql: str) -> list[tuple[Any, ...]]:
        self.query_count += 1
        self.last_sql = sql
        return self._execute(sql)

    def execute_one(self, sql: str) -> tuple[Any, ...]:
        rows = self.execute(sql)
        if not rows:
            raise EngineError("Query returned no rows where exactly one was expected")
        return rows[0]

    @abstractmethod
    def columns(self, relation: Relation) -> list[tuple[str, str]]:
        """Return ``[(column_name, physical_type), ...]`` in ordinal order."""

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    def __enter__(self) -> "Engine":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class EngineError(RuntimeError):
    """Raised when the underlying engine cannot execute or connect."""
