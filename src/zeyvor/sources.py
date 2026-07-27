"""Resolve a user-supplied source string into an Engine plus a Relation.

Accepted forms::

    orders.csv                      local file (csv/tsv/parquet/json/ndjson)
    data/*.parquet                  glob across many files
    https://host/orders.csv         remote file (httpfs)
    s3://bucket/orders.parquet      object storage (httpfs)
    duckdb:///warehouse.db#orders   table inside a DuckDB database
    postgres://user:pw@host/db#public.orders
    mysql://user:pw@host/db#orders
    sqlite:///local.db#orders
    snowflake://ACCOUNT#DB.SCHEMA.TABLE
    bigquery://project#dataset.table

A trailing ``#name`` fragment selects the table for database sources.

One deliberate decision lives here: file sources are read as all-VARCHAR.
Zeyvor does not trust a reader's type guess — it measures the types itself from
the values. Reading as text also means a messy file can never crash profiling,
which matters because messy files are the entire point of the product.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from .engines.base import Engine, EngineError, Relation
from .engines.duckdb_engine import DuckDBEngine

CSV_EXTENSIONS = {".csv", ".tsv", ".txt"}
PARQUET_EXTENSIONS = {".parquet", ".pq"}
JSON_EXTENSIONS = {".json", ".ndjson", ".jsonl"}

REMOTE_SCHEMES = ("https://", "http://", "s3://", "gcs://", "gs://", "az://", "azure://")
DB_SCHEMES = {
    "duckdb": "duckdb",
    "postgres": "postgres",
    "postgresql": "postgres",
    "mysql": "mysql",
    "sqlite": "sqlite",
    "snowflake": "snowflake",
    "bigquery": "bigquery",
}


@dataclass(frozen=True)
class ResolvedSource:
    engine: Engine
    relation: Relation
    owns_engine: bool = True

    def close(self) -> None:
        if self.owns_engine:
            self.engine.close()


def _q(path: str) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _display_name(path: str) -> str:
    base = os.path.basename(path.split("?")[0].rstrip("/")) or path
    stem = os.path.splitext(base)[0]
    cleaned = "".join(c if (c.isalnum() or c == "_") else "_" for c in stem).strip("_")
    return cleaned.lower() or "source"


def _split_fragment(source: str) -> tuple[str, str | None]:
    if "#" in source:
        uri, _, fragment = source.partition("#")
        return uri, (fragment or None)
    return source, None


def _file_relation(path: str, source_uri: str, csv_options: dict[str, Any]) -> Relation:
    """Build read_* expressions for a file or glob."""
    lowered = path.split("?")[0].lower()
    ext = os.path.splitext(lowered)[1]

    if ext in PARQUET_EXTENSIONS:
        expr = f"read_parquet({_q(path)})"
        return Relation(sql=expr, name=_display_name(path), source_uri=source_uri)

    if ext in JSON_EXTENSIONS:
        expr = f"read_json_auto({_q(path)})"
        return Relation(sql=expr, name=_display_name(path), source_uri=source_uri)

    # Default to CSV. all_varchar keeps profiling immune to dirty values; the
    # declared types are discovered separately from a normally-typed read.
    options = {"all_varchar": True, **csv_options}
    if ext == ".tsv":
        options.setdefault("delim", "\t")

    rendered = ", ".join(f"{k}={_render_option(v)}" for k, v in options.items())
    text_expr = (
        f"read_csv_auto({_q(path)}, {rendered})" if rendered else f"read_csv_auto({_q(path)})"
    )

    typed_options = {k: v for k, v in options.items() if k != "all_varchar"}
    typed_rendered = ", ".join(f"{k}={_render_option(v)}" for k, v in typed_options.items())
    typed_expr = (
        f"read_csv_auto({_q(path)}, {typed_rendered})"
        if typed_rendered
        else f"read_csv_auto({_q(path)})"
    )

    return Relation(
        sql=text_expr,
        name=_display_name(path),
        source_uri=source_uri,
        typed_sql=None if typed_expr == text_expr else typed_expr,
    )


def _render_option(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return _q(str(value))


def _qualified_table(dialect_engine: Engine, fragment: str) -> str:
    """Quote a possibly dotted table reference (``schema.table``)."""
    parts = [p for p in fragment.split(".") if p]
    return ".".join(dialect_engine.dialect.quote_ident(p) for p in parts)


def resolve_source(
    source: str,
    *,
    table: str | None = None,
    engine: Engine | None = None,
    csv_options: dict[str, Any] | None = None,
    duckdb_database: str = ":memory:",
    threads: int | None = None,
    memory_limit: str | None = None,
    connect_kwargs: dict[str, Any] | None = None,
) -> ResolvedSource:
    """Turn a source string into something the profiler can aggregate over."""
    source = source.strip()
    if not source:
        raise ValueError("Source must not be empty")

    csv_options = dict(csv_options or {})
    connect_kwargs = dict(connect_kwargs or {})
    uri, fragment = _split_fragment(source)
    fragment = table or fragment

    scheme = uri.split("://", 1)[0].lower() if "://" in uri else ""

    # ── caller-provided engine (tests, reuse, connection pooling) ─────────────
    if engine is not None:
        if fragment:
            relation = Relation(
                sql=_qualified_table(engine, fragment),
                name=fragment.split(".")[-1],
                source_uri=source,
            )
        else:
            relation = _file_relation(uri, source, csv_options)
        return ResolvedSource(engine=engine, relation=relation, owns_engine=False)

    # ── remote files ──────────────────────────────────────────────────────────
    if uri.startswith(REMOTE_SCHEMES):
        eng = DuckDBEngine(
            duckdb_database, extensions=("httpfs",), threads=threads, memory_limit=memory_limit
        )
        return ResolvedSource(engine=eng, relation=_file_relation(uri, source, csv_options))

    # ── databases ─────────────────────────────────────────────────────────────
    if scheme in DB_SCHEMES:
        kind = DB_SCHEMES[scheme]

        if kind == "snowflake":
            from .engines.warehouse import SnowflakeEngine  # noqa: PLC0415

            if not fragment:
                raise ValueError(
                    "Snowflake sources need a table: snowflake://ACCOUNT#DB.SCHEMA.TABLE"
                )
            account = uri.split("://", 1)[1].strip("/")
            eng: Engine = SnowflakeEngine(account=account or None, **connect_kwargs)
            return ResolvedSource(
                engine=eng,
                relation=Relation(
                    sql=_qualified_table(eng, fragment),
                    name=fragment.split(".")[-1],
                    source_uri=source,
                ),
            )

        if kind == "bigquery":
            from .engines.warehouse import BigQueryEngine  # noqa: PLC0415

            if not fragment:
                raise ValueError("BigQuery sources need a table: bigquery://project#dataset.table")
            project = uri.split("://", 1)[1].strip("/") or None
            eng = BigQueryEngine(project=project, **connect_kwargs)
            full = f"{project}.{fragment}" if project and fragment.count(".") == 1 else fragment
            return ResolvedSource(
                engine=eng,
                relation=Relation(
                    sql=_qualified_table(eng, full),
                    name=fragment.split(".")[-1],
                    source_uri=source,
                ),
            )

        if kind == "duckdb":
            # Validate before connecting. DuckDB creates the database file on
            # open, so checking the fragment afterwards would leave a stray file
            # behind every time someone mistyped a source.
            if not fragment:
                raise ValueError("DuckDB sources need a table: duckdb:///file.db#table")
            db_path = uri.split("://", 1)[1]
            db_path = db_path[1:] if db_path.startswith("/") and os.name != "nt" else db_path
            eng = DuckDBEngine(db_path or ":memory:", threads=threads, memory_limit=memory_limit)
            return ResolvedSource(
                engine=eng,
                relation=Relation(
                    sql=_qualified_table(eng, fragment),
                    name=fragment.split(".")[-1],
                    source_uri=source,
                ),
            )

        # Postgres / MySQL / SQLite reached through DuckDB extensions.
        if not fragment:
            raise ValueError(
                f"{kind} sources need a table, e.g. {scheme}://user:pw@host/db#public.orders"
            )
        dsn = _dsn_for(kind, uri)
        eng = DuckDBEngine(
            duckdb_database,
            attach=((dsn, "zeyvor_src", kind),),
            threads=threads,
            memory_limit=memory_limit,
        )
        qualified = (
            fragment if fragment.count(".") >= 1 or kind == "sqlite" else f"public.{fragment}"
        )
        relation_sql = "zeyvor_src." + _qualified_table(eng, qualified)
        return ResolvedSource(
            engine=eng,
            relation=Relation(sql=relation_sql, name=fragment.split(".")[-1], source_uri=source),
        )

    # ── local files ───────────────────────────────────────────────────────────
    has_glob = any(ch in uri for ch in "*?[")
    if not has_glob and not os.path.exists(uri):
        raise FileNotFoundError(f"No such file: {uri}")

    eng = DuckDBEngine(duckdb_database, threads=threads, memory_limit=memory_limit)
    return ResolvedSource(engine=eng, relation=_file_relation(uri, source, csv_options))


def _dsn_for(kind: str, uri: str) -> str:
    """DuckDB's attach DSN formats differ slightly per extension."""
    if kind == "sqlite":
        path = uri.split("://", 1)[1]
        return path[1:] if path.startswith("/") and os.name != "nt" else path
    if kind == "postgres":
        # The postgres extension accepts libpq URIs directly.
        return uri if uri.startswith("postgres") else f"postgresql://{uri}"
    return uri


__all__ = ["ResolvedSource", "resolve_source", "EngineError"]
