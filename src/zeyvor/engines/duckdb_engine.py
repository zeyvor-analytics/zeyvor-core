"""DuckDB execution engine.

DuckDB is the default engine because it reads almost everything in place —
local CSV/Parquet/JSON, files over HTTPS/S3, and (through its own extensions)
live Postgres, MySQL and SQLite databases. Nothing is copied or loaded first;
profiling runs as aggregates over the source.
"""

from __future__ import annotations

from typing import Any

from .base import DuckDBDialect, Engine, EngineError, Relation


class DuckDBEngine(Engine):
    """Runs profiling SQL inside an in-process DuckDB connection."""

    def __init__(
        self,
        database: str = ":memory:",
        *,
        read_only: bool = False,
        extensions: tuple[str, ...] = (),
        attach: tuple[tuple[str, str, str], ...] = (),
        threads: int | None = None,
        memory_limit: str | None = None,
    ) -> None:
        """
        Args:
            database: DuckDB database to connect to; in-memory by default.
            extensions: extensions to INSTALL/LOAD (e.g. ``("httpfs",)``).
            attach: ``(dsn, alias, type)`` triples, e.g.
                ``(("host=... dbname=...", "pg", "postgres"),)``.
            threads: cap DuckDB's thread count (useful inside CI containers).
            memory_limit: e.g. ``"1GB"``. DuckDB otherwise sizes its buffer pool
                against total system RAM, which is the right default on a laptop
                and the wrong one inside a memory-capped CI container. Setting it
                makes DuckDB spill to disk instead of being killed.
        """
        super().__init__()
        try:
            import duckdb  # noqa: PLC0415 - optional-at-import-time by design
        except ImportError as exc:  # pragma: no cover
            raise EngineError(
                "duckdb is required. Install with: pip install zeyvor"
            ) from exc

        self.dialect = DuckDBDialect()
        self._duckdb = duckdb
        try:
            self._conn = duckdb.connect(database, read_only=read_only)
        except Exception as exc:
            raise EngineError(f"Could not open DuckDB database {database!r}: {exc}") from exc

        # A library must not write to its caller's stdout. DuckDB draws a
        # progress bar for long queries, which corrupts piped JSON output and
        # anything a CI job is parsing.
        try:
            self._conn.execute("SET enable_progress_bar = false")
        except Exception:  # pragma: no cover - older builds lack the setting
            pass

        if threads:
            self._conn.execute(f"SET threads TO {int(threads)}")
        if memory_limit:
            safe_limit = "".join(
                c for c in str(memory_limit) if c.isalnum() or c in ".%_ "
            ).strip()
            self._conn.execute(f"SET memory_limit = '{safe_limit}'")

        for ext in extensions:
            self._load_extension(ext)

        for dsn, alias, db_type in attach:
            self.attach(dsn, alias, db_type)

    # ── setup helpers ─────────────────────────────────────────────────────────

    def _load_extension(self, name: str) -> None:
        try:
            self._conn.execute(f"INSTALL {name}")
        except Exception:
            # Already installed, or offline with a bundled copy — LOAD decides.
            pass
        try:
            self._conn.execute(f"LOAD {name}")
        except Exception as exc:
            raise EngineError(f"Could not load DuckDB extension {name!r}: {exc}") from exc

    def attach(self, dsn: str, alias: str, db_type: str) -> None:
        """ATTACH an external database (postgres, mysql, sqlite) by extension."""
        ext = {"postgres": "postgres", "mysql": "mysql", "sqlite": "sqlite"}.get(db_type)
        if ext is None:
            raise EngineError(f"Unsupported attach type: {db_type!r}")
        self._load_extension(ext)
        quoted_dsn = dsn.replace("'", "''")
        safe_alias = "".join(c for c in alias if c.isalnum() or c == "_") or "src"
        try:
            self._conn.execute(
                f"ATTACH IF NOT EXISTS '{quoted_dsn}' AS {safe_alias} (TYPE {db_type.upper()})"
            )
        except Exception as exc:
            raise EngineError(f"Could not attach {db_type} database: {exc}") from exc

    def enable_remote_files(self) -> None:
        """Load httpfs so read_csv_auto() can read https:// and s3:// URLs."""
        self._load_extension("httpfs")

    # ── Engine interface ──────────────────────────────────────────────────────

    def _execute(self, sql: str) -> list[tuple[Any, ...]]:
        try:
            return self._conn.execute(sql).fetchall()
        except Exception as exc:
            raise EngineError(str(exc)) from exc

    def columns(self, relation: Relation) -> list[tuple[str, str]]:
        rows = self.execute(f"DESCRIBE SELECT * FROM {relation.sql}")
        # DESCRIBE → (column_name, column_type, null, key, default, extra)
        return [(str(r[0]), str(r[1])) for r in rows]

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # pragma: no cover
            pass
