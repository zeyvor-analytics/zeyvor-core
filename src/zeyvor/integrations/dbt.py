"""Reading a dbt project's manifest.

dbt already knows what tables exist and where they live, so a user should not
have to repeat that. `target/manifest.json` lists every model with the database,
schema and alias it was built into — enough to point Zeyvor at each one.

What the manifest deliberately does *not* contain is credentials; those live in
`profiles.yml` behind Jinja and environment variables. Rather than reimplement
dbt's own resolution (and get it subtly wrong across versions), the connection is
supplied once on the command line and the manifest supplies the rest:

    zeyvor check --dbt target/manifest.json --warehouse "bigquery://project"

The manifest format has changed across dbt releases, so everything here reads
defensively: fields are fetched by name with fallbacks, never by position, and a
missing optional field is not an error.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

MANIFEST_DEFAULT = os.path.join("target", "manifest.json")


class DbtError(ValueError):
    """A manifest that cannot be used, explained in terms a dbt user knows."""


@dataclass(frozen=True)
class DbtModel:
    name: str
    database: str
    schema: str
    alias: str
    materialized: str = ""
    unique_id: str = ""
    description: str = ""

    @property
    def relation(self) -> str:
        """`schema.alias`, or `database.schema.alias` where a database is known."""
        parts = [p for p in (self.database, self.schema, self.alias) if p]
        return ".".join(parts)

    def source_uri(self, warehouse: str) -> str:
        """Combine the connection with this model's location.

        `warehouse` is everything before the fragment — `bigquery://project`,
        `postgres://user:pw@host/db`, `duckdb:///warehouse.duckdb`.
        """
        base = warehouse.rstrip("#")
        if "#" in warehouse:
            raise DbtError(
                "--warehouse should not include a '#table' fragment; the manifest "
                "supplies the table."
            )
        return f"{base}#{self.qualified_for(base)}"

    def qualified_for(self, warehouse: str) -> str:
        """How much qualification a given backend wants.

        DuckDB and Postgres reached through an attached database already know
        which database they are in, so a three-part name would not resolve.
        """
        scheme = warehouse.split("://", 1)[0].lower() if "://" in warehouse else ""
        if scheme in {"duckdb", "postgres", "postgresql", "mysql", "sqlite"}:
            return ".".join(p for p in (self.schema, self.alias) if p)
        return self.relation


def load_manifest(path: str = MANIFEST_DEFAULT) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise DbtError(
            f"No dbt manifest at {path}. Run `dbt compile` or `dbt run` first."
        ) from None
    except json.JSONDecodeError as exc:
        raise DbtError(f"{path} is not valid JSON: {exc}") from None


def manifest_version(manifest: dict[str, Any]) -> str:
    metadata = manifest.get("metadata") or {}
    return str(metadata.get("dbt_schema_version") or metadata.get("dbt_version") or "unknown")


def models(
    manifest: dict[str, Any],
    *,
    select: list[str] | None = None,
    include_ephemeral: bool = False,
) -> list[DbtModel]:
    """Every model in the manifest, as things Zeyvor can point at.

    Ephemeral models are excluded by default because they are inlined as CTEs and
    have no table to check. Seeds and snapshots are included: they are real
    tables, and a seed drifting is exactly the sort of quiet breakage worth
    catching.
    """
    nodes = manifest.get("nodes")
    if not isinstance(nodes, dict):
        raise DbtError(
            "This manifest has no 'nodes' section — it may be from a very old "
            "dbt, or not a manifest at all."
        )

    wanted = set(select or [])
    out: list[DbtModel] = []
    all_names: list[str] = []
    for unique_id, node in nodes.items():
        if not isinstance(node, dict):
            continue
        if node.get("resource_type") not in {"model", "seed", "snapshot"}:
            continue

        config = node.get("config") or {}
        materialized = str(config.get("materialized") or node.get("materialized") or "")
        if materialized == "ephemeral" and not include_ephemeral:
            continue

        name = str(node.get("name") or "")
        all_names.append(name)
        if wanted and name not in wanted and unique_id not in wanted:
            continue

        out.append(
            DbtModel(
                name=name,
                database=str(node.get("database") or ""),
                schema=str(node.get("schema") or ""),
                # alias is what the table is actually called; it defaults to name
                # but a model can override it, and checking the wrong table would
                # be a silent no-op.
                alias=str(node.get("alias") or name),
                materialized=materialized,
                unique_id=str(unique_id),
                description=str(node.get("description") or ""),
            )
        )

    if wanted:
        found = {m.name for m in out} | {m.unique_id for m in out}
        missing = sorted(wanted - found)
        if missing:
            # List everything in the manifest, not what survived the filter —
            # the filter matched nothing, which is exactly why we are here.
            available = ", ".join(sorted(set(all_names))[:12]) or "none"
            raise DbtError(f"No such model(s): {', '.join(missing)}. Available: {available}")

    return sorted(out, key=lambda m: m.name)


def sources_for(
    manifest: dict[str, Any],
    warehouse: str,
    *,
    select: list[str] | None = None,
) -> list[tuple[str, str]]:
    """`(table name, source uri)` for each selected model."""
    if not warehouse:
        raise DbtError(
            "A dbt manifest says which tables exist but not how to reach them. "
            'Pass the connection: --warehouse "bigquery://project"'
        )
    return [(model.name, model.source_uri(warehouse)) for model in models(manifest, select=select)]


__all__ = [
    "MANIFEST_DEFAULT",
    "DbtError",
    "DbtModel",
    "load_manifest",
    "manifest_version",
    "models",
    "sources_for",
]
