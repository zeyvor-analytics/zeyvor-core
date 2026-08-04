"""Engine behaviour and error handling.

Warehouse adapters cannot be connected to offline, so what is tested here is
everything around them: query accounting, and whether a failure tells the user
what to do next. A confusing error at install time is an adoption problem, not
an edge case.
"""

from __future__ import annotations

import pytest

from zeyvor import DuckDBEngine, EngineError
from zeyvor.engines.base import Relation
from zeyvor.engines.warehouse import BigQueryEngine, DBAPIEngine, _type_name

# ── query accounting ──────────────────────────────────────────────────────────


def test_queries_are_counted():
    """The count is what lets tests prove pushdown, so it has to be honest."""
    engine = DuckDBEngine()
    try:
        assert engine.query_count == 0
        engine.execute("SELECT 1")
        engine.execute("SELECT 2")
        assert engine.query_count == 2
        assert engine.last_sql == "SELECT 2"
    finally:
        engine.close()


def test_execute_one_returns_a_single_row(duck):
    assert duck.execute_one("SELECT 42, 'x'") == (42, "x")


def test_execute_one_fails_clearly_when_nothing_comes_back(duck):
    with pytest.raises(EngineError) as excinfo:
        duck.execute_one("SELECT 1 WHERE 1 = 0")
    assert "one" in str(excinfo.value).lower()


def test_invalid_sql_surfaces_as_engine_error(duck):
    with pytest.raises(EngineError):
        duck.execute("SELECT FROM WHERE")


def test_columns_lists_names_and_types_in_order(duck):
    relation = Relation(
        sql="(SELECT 1::BIGINT AS a, 'x' AS b, DATE '2024-01-01' AS c)",
        name="t",
        source_uri="test",
    )
    columns = duck.columns(relation)
    assert [name for name, _ in columns] == ["a", "b", "c"]
    assert columns[0][1] == "BIGINT"
    assert columns[2][1] == "DATE"


# ── setup failures ────────────────────────────────────────────────────────────


def test_unsupported_attach_type_names_the_problem():
    engine = DuckDBEngine()
    try:
        with pytest.raises(EngineError) as excinfo:
            engine.attach("dsn", "alias", "oracle")
        assert "oracle" in str(excinfo.value)
    finally:
        engine.close()


def test_attach_alias_is_sanitised(tmp_path):
    """An alias reaches SQL as an identifier, so it cannot be trusted verbatim."""
    engine = DuckDBEngine()
    try:
        engine.execute("CREATE TABLE keep_me AS SELECT 1 AS a")
        engine.attach(str(tmp_path / "side.db"), "bad; DROP TABLE keep_me", "sqlite")

        # The punctuation was stripped rather than executed.
        databases = {
            row[0] for row in engine.execute("SELECT database_name FROM duckdb_databases()")
        }
        assert "badDROPTABLEkeep_me" in databases
        assert not any(";" in name for name in databases)

        # And the injected statement plainly did not run.
        assert engine.execute_one("SELECT COUNT(*) FROM keep_me")[0] == 1
    finally:
        engine.close()


def test_unopenable_database_fails_with_context():
    with pytest.raises(EngineError) as excinfo:
        DuckDBEngine("/definitely/not/a/directory/db.duckdb")
    assert "DuckDB" in str(excinfo.value)


# ── warehouse adapters ────────────────────────────────────────────────────────


def test_missing_bigquery_driver_tells_you_the_extra_to_install():
    try:
        from google.cloud import bigquery  # noqa: F401, PLC0415

        pytest.skip("bigquery driver is installed; the guidance path cannot run")
    except ImportError:
        pass

    with pytest.raises(EngineError) as excinfo:
        BigQueryEngine(project="x")
    message = str(excinfo.value)
    assert "zeyvor[bigquery]" in message and "pip install" in message


class FakeCursor:
    description = (("id", int), ("name", str))

    def __init__(self, rows):
        self._rows = rows
        self.executed: list[str] = []

    def execute(self, sql):
        self.executed.append(sql)

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class FakeConnection:
    def __init__(self, rows):
        self.cursor_obj = FakeCursor(rows)
        self.closed = False

    def cursor(self):
        return self.cursor_obj

    def close(self):
        self.closed = True


def test_dbapi_engine_executes_and_describes():
    """The generic PEP 249 path is what every future warehouse rides on."""
    from zeyvor.engines.base import PostgresDialect

    connection = FakeConnection([(1, "a"), (2, "b")])
    engine = DBAPIEngine(connection, PostgresDialect())

    assert engine.execute("SELECT 1") == [(1, "a"), (2, "b")]
    assert engine.query_count == 1

    relation = Relation(sql='"orders"', name="orders", source_uri="test")
    assert engine.columns(relation) == [("id", "int"), ("name", "str")]
    assert "WHERE 1 = 0" in connection.cursor_obj.executed[-1]

    engine.close()
    assert connection.closed


def test_dbapi_failures_become_engine_errors():
    from zeyvor.engines.base import PostgresDialect

    class Broken:
        def cursor(self):
            raise RuntimeError("connection reset")

    engine = DBAPIEngine(Broken(), PostgresDialect())
    with pytest.raises(EngineError) as excinfo:
        engine.execute("SELECT 1")
    assert "connection reset" in str(excinfo.value)


def test_type_name_handles_whatever_a_driver_provides():
    assert _type_name(None) == "unknown"
    assert _type_name(str) == "str"
    assert _type_name("VARCHAR") == "VARCHAR"

    class Coded:
        name = "NUMBER"

    assert _type_name(Coded()) == "NUMBER"


# ── context manager ───────────────────────────────────────────────────────────


def test_engine_works_as_a_context_manager():
    with DuckDBEngine() as engine:
        assert engine.execute_one("SELECT 1")[0] == 1


# ── resource limits ───────────────────────────────────────────────────────────


def test_memory_limit_and_threads_are_applied():
    """CI containers cap memory; DuckDB otherwise sizes against host RAM."""
    engine = DuckDBEngine(memory_limit="512MB", threads=2)
    try:
        settings = dict(
            engine.execute(
                "SELECT name, value FROM duckdb_settings() "
                "WHERE name IN ('memory_limit', 'threads')"
            )
        )
        assert settings["threads"] == "2"
        assert "MiB" in settings["memory_limit"] or "MB" in settings["memory_limit"]
    finally:
        engine.close()


def test_memory_limit_cannot_smuggle_sql():
    engine = DuckDBEngine(memory_limit="512MB'; DROP TABLE x; --")
    try:
        assert engine.execute_one("SELECT 1")[0] == 1
    finally:
        engine.close()


def test_progress_bar_is_disabled():
    """A library writing progress bars to stdout corrupts piped JSON."""
    engine = DuckDBEngine()
    try:
        (value,) = engine.execute_one(
            "SELECT value FROM duckdb_settings() WHERE name = 'enable_progress_bar'"
        )
        assert value == "false"
    finally:
        engine.close()
