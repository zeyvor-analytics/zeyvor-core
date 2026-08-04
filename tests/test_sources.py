"""Source resolution: turning a string a human typed into something queryable."""

from __future__ import annotations

import pytest

from helpers import fixture_path
from zeyvor.sources import _display_name, _dsn_for, _expand_env, _split_fragment, resolve_source

# ── files ─────────────────────────────────────────────────────────────────────


def test_local_csv_reads_as_text_for_measurement():
    """All-VARCHAR is deliberate: a bad value must never break profiling."""
    resolved = resolve_source(fixture_path("clean_orders.csv"))
    try:
        assert "read_csv_auto" in resolved.relation.sql
        assert "all_varchar=true" in resolved.relation.sql
        # A second, normally-typed expression exists purely to report the types
        # the source itself claims.
        assert resolved.relation.typed_sql is not None
        assert "all_varchar" not in resolved.relation.typed_sql
    finally:
        resolved.close()


def test_relation_name_is_derived_from_the_filename():
    resolved = resolve_source(fixture_path("clean_orders.csv"))
    try:
        assert resolved.relation.name == "clean_orders"
    finally:
        resolved.close()


def test_display_name_sanitises_awkward_filenames():
    assert _display_name("/tmp/Q4 Sales (final).csv") == "q4_sales__final"
    assert _display_name("https://host/path/orders.csv?token=abc") == "orders"
    assert _display_name("data.2024.parquet") == "data_2024"


def test_parquet_uses_the_parquet_reader():
    # Resolution must not require the file to be readable, only addressable.
    with pytest.raises(FileNotFoundError):
        resolve_source("/nope/data.parquet")


def test_glob_is_accepted_without_touching_the_filesystem():
    resolved = resolve_source(fixture_path("").rstrip("/") + "/*.csv")
    try:
        assert "*" in resolved.relation.sql
    finally:
        resolved.close()


def test_missing_file_fails_immediately_and_clearly():
    with pytest.raises(FileNotFoundError) as excinfo:
        resolve_source("/definitely/not/here.csv")
    assert "not/here.csv" in str(excinfo.value)


def test_empty_source_is_rejected():
    with pytest.raises(ValueError):
        resolve_source("   ")


# ── fragments ─────────────────────────────────────────────────────────────────


def test_fragment_selects_the_table():
    assert _split_fragment("postgres://host/db#public.orders") == (
        "postgres://host/db",
        "public.orders",
    )
    assert _split_fragment("orders.csv") == ("orders.csv", None)


def test_a_rejected_source_leaves_nothing_behind(tmp_path, monkeypatch):
    """Validation must not have side effects.

    DuckDB creates a database file the moment it is opened, so a mistyped
    source once left a stray file on disk before raising.
    """
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError):
        resolve_source("duckdb:///stray.db")
    assert list(tmp_path.iterdir()) == []


def test_database_sources_require_a_table():
    for uri in (
        "postgres://user:pw@localhost/db",
        "duckdb:///warehouse.db",
        "bigquery://project",
    ):
        with pytest.raises(ValueError) as excinfo:
            resolve_source(uri)
        assert "table" in str(excinfo.value).lower()


# ── supplied engines ──────────────────────────────────────────────────────────


def test_an_existing_engine_is_reused_and_not_closed(duck):
    resolved = resolve_source(fixture_path("clean_orders.csv"), engine=duck)
    assert resolved.owns_engine is False
    resolved.close()
    # The session engine must still be usable afterwards.
    assert duck.execute_one("SELECT 1")[0] == 1


def test_table_argument_overrides_the_fragment(duck):
    duck.execute("CREATE OR REPLACE TABLE demo_orders AS SELECT 1 AS a")
    resolved = resolve_source("ignored", table="demo_orders", engine=duck)
    assert resolved.relation.sql == '"demo_orders"'
    assert resolved.relation.name == "demo_orders"


def test_dotted_table_names_are_quoted_per_part(duck):
    resolved = resolve_source("x", table="main.demo_orders", engine=duck)
    assert resolved.relation.sql == '"main"."demo_orders"'


# ── credentials in database sources ────────────────────────────────────────────
#
# Found by a real report: `zeyvor init "postgres://user:realpassword@host/db#t"`
# wrote that password verbatim into the committed contract's `source:` line —
# exactly the file the whole workflow says to commit and review like code. A
# rotated password then meant a stale credential baked into version control,
# with no way to fix it except regenerating the file. ${VAR} placeholders let
# the committed file hold no secret at all, and a rotation become an
# environment-variable change instead of a file change.


def test_env_placeholders_expand_from_the_environment(monkeypatch):
    monkeypatch.setenv("DB_PASSWORD", "hunter2")
    assert (
        _expand_env("postgres://user:${DB_PASSWORD}@host/db") == "postgres://user:hunter2@host/db"
    )


def test_a_source_with_no_placeholders_is_unaffected():
    assert _expand_env("postgres://user:pw@host/db") == "postgres://user:pw@host/db"


def test_a_missing_variable_fails_clearly_rather_than_connecting_with_the_literal_text(
    monkeypatch,
):
    monkeypatch.delenv("DB_PASSWORD", raising=False)
    with pytest.raises(ValueError, match=r"\$\{DB_PASSWORD\} is not set"):
        _expand_env("postgres://user:${DB_PASSWORD}@host/db")


def test_the_actual_connection_uses_the_expanded_dsn(monkeypatch):
    """This is the half that has to work for the feature to be more than a
    trick: whatever gets attached for real must be the real password."""
    monkeypatch.setenv("DB_PASSWORD", "hunter2")
    dsn = _dsn_for("postgres", "postgres://user:${DB_PASSWORD}@host/db")
    assert dsn == "postgres://user:hunter2@host/db"


def test_the_committed_source_keeps_the_placeholder_not_the_secret(tmp_path, monkeypatch):
    """The other half: what a reviewer sees in the diff must never be the
    credential, regardless of what the live connection actually used.

    Exercised through a real connection (sqlite, so no network is involved)
    rather than mocked, since the thing being proven is that the whole
    round trip — resolve, connect, and record — keeps them separate.
    """
    import sqlite3

    db_path = tmp_path / "t.db"
    conn = sqlite3.connect(db_path)
    conn.execute("create table orders (id integer primary key)")
    conn.commit()
    conn.close()

    monkeypatch.setenv("DB_DIR", str(tmp_path))
    literal = "sqlite:///${DB_DIR}/t.db#orders"

    resolved = resolve_source(literal)
    try:
        assert resolved.relation.source_uri == literal
        assert str(tmp_path) not in resolved.relation.source_uri
        assert resolved.engine.execute("select * from " + resolved.relation.sql) == []
    finally:
        resolved.close()
