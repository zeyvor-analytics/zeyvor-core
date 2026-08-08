"""Zeyvor — semantic data quality.

Your tests check that the boxes are filled in. Zeyvor checks that what is in
the box still matches the label on it.

Part 1 (this module) is measurement: point it at data, get back a structured,
privacy-safe profile of what the values actually are.

    from zeyvor import profile_source

    profile = profile_source("orders.csv")
    print(profile.column("signup_date").inferred_type)      # InferredType.DATE
    print(profile.column("notes").observations)             # ['pii_in_free_text']
    print(profile.to_json())

Nothing here fetches a row. Every number is a SQL aggregate executed where the
data already lives, which is what allows the same code to profile a 200-row CSV
and a billion-row warehouse table.
"""

from .engines.base import Dialect, Engine, EngineError, Relation
from .engines.duckdb_engine import DuckDBEngine
from .profile import (
    ColumnProfile,
    InferredType,
    Observation,
    PrivacyMode,
    ProfileOptions,
    Profiler,
    TableProfile,
    profile_source,
)
from .sources import resolve_source

__version__ = "0.5.0"

__all__ = [
    "__version__",
    "ColumnProfile",
    "Dialect",
    "DuckDBEngine",
    "Engine",
    "EngineError",
    "InferredType",
    "Observation",
    "PrivacyMode",
    "ProfileOptions",
    "Profiler",
    "Relation",
    "TableProfile",
    "profile_source",
    "resolve_source",
]
