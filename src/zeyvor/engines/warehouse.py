"""Warehouse engines: profiling SQL executed by the warehouse itself.

BigQuery has no DuckDB extension, and copying a fact table to a laptop to
measure it would defeat the point. Instead the same generated aggregates are
sent to the warehouse over its own driver, so the data never moves and the
customer pays only for a few scan queries.

These adapters are deliberately thin. They are exercised against real
warehouses rather than in the unit-test suite, which runs offline.
"""

from __future__ import annotations

import contextlib
from typing import Any

from .base import BigQueryDialect, Engine, EngineError, Relation


class DBAPIEngine(Engine):
    """Generic PEP 249 engine for any driver exposing cursor()/execute()."""

    def __init__(self, connection: Any, dialect: Any) -> None:
        super().__init__()
        self._conn = connection
        self.dialect = dialect

    def _execute(self, sql: str) -> list[tuple[Any, ...]]:
        try:
            cur = self._conn.cursor()
            cur.execute(sql)
            rows = cur.fetchall()
            cur.close()
            return [tuple(r) for r in rows]
        except Exception as exc:
            raise EngineError(str(exc)) from exc

    def columns(self, relation: Relation) -> list[tuple[str, str]]:
        """Read column names and types from the driver's cursor description."""
        try:
            cur = self._conn.cursor()
            cur.execute(f"SELECT * FROM {relation.sql} WHERE 1 = 0")
            description = cur.description or []
            cur.close()
        except Exception as exc:
            raise EngineError(f"Could not describe {relation.name}: {exc}") from exc

        out: list[tuple[str, str]] = []
        for item in description:
            name = str(item[0])
            type_code = item[1] if len(item) > 1 else None
            out.append((name, _type_name(type_code)))
        return out

    def close(self) -> None:
        with contextlib.suppress(Exception):  # pragma: no cover
            self._conn.close()


class BigQueryEngine(Engine):
    """BigQuery speaks its own client API rather than PEP 249."""

    def __init__(self, project: str | None = None, **client_kwargs: Any) -> None:
        super().__init__()
        try:
            from google.cloud import bigquery  # noqa: PLC0415
        except ImportError as exc:
            raise EngineError("BigQuery support requires: pip install 'zeyvor[bigquery]'") from exc
        try:
            self._client = bigquery.Client(project=project, **client_kwargs)
        except Exception as exc:
            raise EngineError(f"Could not create BigQuery client: {exc}") from exc
        self.dialect = BigQueryDialect()

    def _execute(self, sql: str) -> list[tuple[Any, ...]]:
        try:
            return [tuple(row.values()) for row in self._client.query(sql).result()]
        except Exception as exc:
            raise EngineError(str(exc)) from exc

    def columns(self, relation: Relation) -> list[tuple[str, str]]:
        try:
            job = self._client.query(f"SELECT * FROM {relation.sql} LIMIT 0")
            return [(f.name, f.field_type) for f in job.result().schema]
        except Exception as exc:
            raise EngineError(f"Could not describe {relation.name}: {exc}") from exc


def _type_name(type_code: Any) -> str:
    """Best-effort physical type name from a DBAPI type code."""
    if type_code is None:
        return "unknown"
    for attr in ("name", "__name__"):
        value = getattr(type_code, attr, None)
        if isinstance(value, str):
            return value
    return str(type_code)
