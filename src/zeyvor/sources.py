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
    bigquery://project#dataset.table

A trailing ``#name`` fragment selects the table for database sources.

One deliberate decision lives here: file sources are read as all-VARCHAR.
Zeyvor does not trust a reader's type guess — it measures the types itself from
the values. Reading as text also means a messy file can never crash profiling,
which matters because messy files are the entire point of the product.

A second: ``${VAR}`` inside a database source is expanded from the environment
at the moment a connection is made, and only there. `resolve_source` still
records the literal, unexpanded string as `source_uri` — the thing that ends
up written into the committed contract — so a connection string typed as
``postgres://${DB_USER}:${DB_PASSWORD}@host/db#public.orders`` commits exactly
that, placeholders and all, never the credential. A rotated password then
means a changed environment variable, not a changed, re-reviewed file.
"""

from __future__ import annotations

import fnmatch
import os
import re
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


# Schemas a warehouse keeps for itself. A wildcard means "my tables", and
# without this `#*` on Postgres profiles roughly two hundred pg_catalog and
# information_schema relations before reaching anything the user owns.
SYSTEM_SCHEMAS = frozenset(
    {
        "information_schema",
        "pg_catalog",
        "pg_toast",
        "sys",
        "mysql",
        "performance_schema",
        "sqlite_master",
        "temp",
    }
)


def expand_tables(
    source: str,
    *,
    threads: int | None = None,
    memory_limit: str | None = None,
    connect_kwargs: dict[str, Any] | None = None,
) -> list[str]:
    """Turn one source whose table part contains ``*`` into one source per table.

    A data model of any size is the normal case, not the exotic one, and naming
    two hundred tables on a command line is not a workflow anybody sustains.
    ``#public.*`` asks the database what it holds and profiles all of it;
    ``#*`` covers every user schema; ``#public.stg_*`` covers a prefix, which is
    how most warehouses distinguish layers.

    Anything without a ``*`` is returned untouched, so this is safe to call on
    every source unconditionally.
    """
    source = source.strip()
    uri, fragment = _split_fragment(source)
    if not fragment or "*" not in fragment:
        return [source]

    scheme = uri.split("://", 1)[0].lower() if "://" in uri else ""
    kind = DB_SCHEMES.get(scheme)
    if kind is None:
        raise ValueError(
            "A table wildcard only works on a database source, e.g. "
            'postgres://user:pw@host/db#public.* — for files, use a shell glob: "data/*.csv"'
        )
    if kind == "bigquery":
        raise ValueError(
            "BigQuery does not support a table wildcard yet. Name the tables, or use "
            "--dbt if they are dbt models."
        )

    if "." in fragment:
        schema_pattern, _, table_pattern = fragment.rpartition(".")
    else:
        schema_pattern, table_pattern = "*", fragment

    engine, catalog = _open_for_discovery(kind, uri, threads, memory_limit, connect_kwargs)
    try:
        rows = engine.execute(
            "SELECT schema_name, table_name FROM duckdb_tables() "
            f"WHERE database_name = {_q(catalog)} ORDER BY schema_name, table_name"
        )
    finally:
        engine.close()

    matched = [
        (schema, table)
        for schema, table in rows
        if schema not in SYSTEM_SCHEMAS
        and fnmatch.fnmatch(schema, schema_pattern)
        and fnmatch.fnmatch(table, table_pattern)
    ]
    if not matched:
        raise ValueError(
            f"No tables matched '{fragment}'. Check the schema name — a Postgres "
            "database usually keeps user tables in 'public'."
        )
    # sqlite has exactly one schema and calls it `main`; carrying that into the
    # table name would make every contract read `main.orders`.
    single_schema = len({schema for schema, _ in matched}) == 1
    return [
        f"{uri}#{table if single_schema and schema in ('main', 'public') else f'{schema}.{table}'}"
        for schema, table in matched
    ]


def _open_for_discovery(
    kind: str,
    uri: str,
    threads: int | None,
    memory_limit: str | None,
    connect_kwargs: dict[str, Any] | None,
) -> tuple[Engine, str]:
    """An engine that can list the source's tables, plus the catalog to look in.

    DuckDB files are opened directly and so list under their own name; everything
    else is reached through an ATTACH and lists under the alias.
    """
    if kind == "duckdb":
        path = _path_from_uri(_expand_env(uri))
        engine = DuckDBEngine(path or ":memory:", threads=threads, memory_limit=memory_limit)
        rows = engine.execute(
            "SELECT database_name FROM duckdb_databases() WHERE NOT internal LIMIT 1"
        )
        return engine, (rows[0][0] if rows else "memory")

    dsn = _dsn_for(kind, uri)
    engine = DuckDBEngine(
        ":memory:",
        attach=((dsn, "zeyvor_src", kind),),
        threads=threads,
        memory_limit=memory_limit,
    )
    return engine, "zeyvor_src"


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

        if kind == "bigquery":
            from .engines.warehouse import BigQueryEngine  # noqa: PLC0415

            if not fragment:
                raise ValueError("BigQuery sources need a table: bigquery://project#dataset.table")
            project = uri.split("://", 1)[1].strip("/") or None
            eng: Engine = BigQueryEngine(project=project, **connect_kwargs)
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
            db_path = _path_from_uri(uri)
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


def _path_from_uri(uri: str) -> str:
    """The filesystem path in a `scheme:///path` URI, on any platform.

    Three slashes means an empty host and the path `/name`, so one leading slash
    always comes off. This used to be written inline in two places, both with an
    `os.name != "nt"` guard that kept the slash on Windows — DuckDB was then
    handed `//name` and rejected it. Stripping is right everywhere: a Windows
    absolute URI is `sqlite:///C:/data/app.db`, and `C:/data/app.db` is exactly
    the path to open.
    """
    path = uri.split("://", 1)[1]
    return path[1:] if path.startswith("/") else path


_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env(value: str) -> str:
    """Expand ``${VAR}`` placeholders from the environment.

    Called once, right before a connection string becomes an actual connection
    — never before. Whatever the caller originally typed, placeholders and all,
    is what gets recorded as the source; this function's output is used to
    connect and then discarded.
    """

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        try:
            return os.environ[name]
        except KeyError:
            raise ValueError(
                f"${{{name}}} is not set. Export it, or use a literal value if this "
                "connection string will not be committed anywhere."
            ) from None

    return _ENV_VAR_PATTERN.sub(replace, value)


def _dsn_for(kind: str, uri: str) -> str:
    """DuckDB's attach DSN formats differ slightly per extension."""
    uri = _expand_env(uri)
    if kind == "sqlite":
        return _path_from_uri(uri)
    if kind == "postgres":
        # The postgres extension accepts libpq URIs directly.
        return uri if uri.startswith("postgres") else f"postgresql://{uri}"
    return uri


__all__ = ["ResolvedSource", "expand_tables", "resolve_source", "EngineError"]
