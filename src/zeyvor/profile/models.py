"""The profile data model — the interface between measurement and judgement.

Part 1 of Zeyvor produces a `TableProfile`. Everything downstream (contract
generation, violation detection, the dashboard) consumes one. Keeping this
boundary explicit and serialisable is what allows the AI step to reason about a
dataset it has never seen a single row of.

Design rules for anything added here:

* JSON round-trippable, no Python-only types.
* Counts are raw integers; rates are derived and rounded for readability.
* Nothing that could contain a raw customer value unless `privacy.py` allows it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

PROFILE_SCHEMA_VERSION = 1


class InferredType(str, Enum):
    """What the values *are*, as distinct from what the schema claims."""

    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    DATE = "date"
    TIMESTAMP = "timestamp"
    EMAIL = "email"
    URL = "url"
    UUID = "uuid"
    JSON = "json"
    TEXT = "text"
    EMPTY = "empty"
    MIXED = "mixed"


class Observation(str, Enum):
    """Machine-readable findings, not prose.

    These are the raw material for Part 2's violation taxonomy: each one is a
    fact about the data that a contract can be written against or checked
    against. Keeping them as keys (rather than English sentences) means the
    diff engine can act on them without parsing text.
    """

    EMPTY_COLUMN = "empty_column"
    CONSTANT = "constant"
    UNIQUE = "unique"
    HIGH_NULL_RATE = "high_null_rate"
    MIXED_TYPES = "mixed_types"
    NUMERIC_STORED_AS_TEXT = "numeric_stored_as_text"
    TEMPORAL_STORED_AS_TEXT = "temporal_stored_as_text"
    BOOLEAN_STORED_AS_TEXT = "boolean_stored_as_text"
    MIXED_BOOLEAN_ENCODING = "mixed_boolean_encoding"
    EPOCH_SUSPECTED = "epoch_suspected"
    EXCEL_SERIAL_SUSPECTED = "excel_serial_suspected"
    LEADING_ZEROS = "leading_zeros"
    CURRENCY_IN_TEXT = "currency_in_text"
    PERCENT_IN_TEXT = "percent_in_text"
    THOUSANDS_SEPARATORS = "thousands_separators"
    NULL_WORDS = "null_words"
    MOJIBAKE = "mojibake"
    WHITESPACE_PADDING = "whitespace_padding"
    INCONSISTENT_CASE = "inconsistent_case"
    MULTIPLE_DATE_FORMATS = "multiple_date_formats"
    ENUM_CANDIDATE = "enum_candidate"
    PII_DETECTED = "pii_detected"
    PII_IN_FREE_TEXT = "pii_in_free_text"
    DECLARED_TYPE_CONFLICT = "declared_type_conflict"


def _round(value: float | None, places: int = 6) -> float | None:
    return None if value is None else round(float(value), places)


def _rate(numerator: int | None, denominator: int | None) -> float | None:
    if not denominator or numerator is None:
        return None
    return round(numerator / denominator, 6)


def _prune(data: dict[str, Any]) -> dict[str, Any]:
    """Drop empty values so profiles stay readable as committed artefacts."""
    return {
        k: v for k, v in data.items() if v is not None and v != [] and v != {} and v is not False
    }


@dataclass
class NumericStats:
    minimum: float | None = None
    maximum: float | None = None
    mean: float | None = None
    stddev: float | None = None
    p25: float | None = None
    p50: float | None = None
    p75: float | None = None
    zero_count: int = 0
    negative_count: int = 0
    integral_count: int = 0
    """Numeric values with no fractional part — distinguishes a true float
    column from integers that a reader happened to widen to DOUBLE."""
    parseable_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return _prune(
            {
                "minimum": _round(self.minimum),
                "maximum": _round(self.maximum),
                "mean": _round(self.mean, 4),
                "stddev": _round(self.stddev, 4),
                "p25": _round(self.p25),
                "p50": _round(self.p50),
                "p75": _round(self.p75),
                "zero_count": self.zero_count or None,
                "negative_count": self.negative_count or None,
                "integral_count": self.integral_count or None,
                "parseable_count": self.parseable_count or None,
            }
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NumericStats:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class TextStats:
    min_length: int | None = None
    max_length: int | None = None
    mean_length: float | None = None
    whitespace_padded_count: int = 0
    uppercase_count: int = 0
    lowercase_count: int = 0
    mixed_case_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return _prune(
            {
                "min_length": self.min_length,
                "max_length": self.max_length,
                "mean_length": _round(self.mean_length, 2),
                "whitespace_padded_count": self.whitespace_padded_count or None,
                "uppercase_count": self.uppercase_count or None,
                "lowercase_count": self.lowercase_count or None,
                "mixed_case_count": self.mixed_case_count or None,
            }
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TextStats:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class TemporalStats:
    minimum: str | None = None
    maximum: str | None = None
    future_count: int = 0
    distant_past_count: int = 0
    parseable_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return _prune(
            {
                "minimum": self.minimum,
                "maximum": self.maximum,
                "future_count": self.future_count or None,
                "distant_past_count": self.distant_past_count or None,
                "parseable_count": self.parseable_count or None,
            }
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TemporalStats:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class ShapeBucket:
    """One formatting signature and how often it occurs."""

    shape: str
    count: int
    rate: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return _prune({"shape": self.shape, "count": self.count, "rate": self.rate})

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ShapeBucket:
        return cls(shape=data["shape"], count=data["count"], rate=data.get("rate"))


@dataclass
class EnumMember:
    value: str
    count: int
    rate: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return _prune({"value": self.value, "count": self.count, "rate": self.rate})

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EnumMember:
        return cls(value=data["value"], count=data["count"], rate=data.get("rate"))


@dataclass
class EnumProfile:
    members: list[EnumMember] = field(default_factory=list)
    complete: bool = True
    """True when every distinct value is listed. A contract can only assert a
    closed category set — and therefore only detect a *new* category — when the
    recorded set was complete."""
    cardinality: int = 0
    hashed: bool = False

    def values(self) -> list[str]:
        return [m.value for m in self.members]

    def to_dict(self) -> dict[str, Any]:
        return _prune(
            {
                "members": [m.to_dict() for m in self.members],
                "complete": self.complete or None,
                "cardinality": self.cardinality,
                "hashed": self.hashed or None,
            }
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EnumProfile:
        return cls(
            members=[EnumMember.from_dict(m) for m in data.get("members", [])],
            complete=bool(data.get("complete", False)),
            cardinality=int(data.get("cardinality", 0)),
            hashed=bool(data.get("hashed", False)),
        )


@dataclass
class ColumnProfile:
    name: str
    position: int = 0

    declared_type: str = "unknown"
    """What the source says the type is (DuckDB's detection for files, the real
    DDL type for databases)."""

    inferred_type: InferredType = InferredType.TEXT
    """What the values actually are, measured by cast probes."""

    type_confidence: float = 0.0
    type_mixture: dict[str, float] = field(default_factory=dict)
    type_probes: dict[str, int] = field(default_factory=dict)

    row_count: int = 0
    null_count: int = 0
    blank_count: int = 0
    distinct_count: int = 0
    distinct_is_approx: bool = False

    pattern_hits: dict[str, int] = field(default_factory=dict)

    numeric: NumericStats | None = None
    text: TextStats | None = None
    temporal: TemporalStats | None = None

    shapes: list[ShapeBucket] = field(default_factory=list)
    shape_distinct_count: int | None = None

    enum: EnumProfile | None = None
    samples: list[str] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)

    # ── derived conveniences ──────────────────────────────────────────────────

    @property
    def non_null_count(self) -> int:
        return max(self.row_count - self.null_count, 0)

    @property
    def valued_count(self) -> int:
        """Non-null values that are not blank.

        This is the denominator for every type and pattern share. An empty
        string is present but meaningless, so counting it would quietly drag
        every confidence score down and make a clean column look mixed.
        """
        return max(self.non_null_count - self.blank_count, 0)

    @property
    def null_rate(self) -> float | None:
        return _rate(self.null_count, self.row_count)

    @property
    def distinct_rate(self) -> float | None:
        return _rate(self.distinct_count, self.non_null_count)

    @property
    def is_unique(self) -> bool:
        return self.non_null_count > 0 and self.distinct_count == self.non_null_count

    @property
    def is_constant(self) -> bool:
        return self.non_null_count > 0 and self.distinct_count == 1

    @property
    def is_empty(self) -> bool:
        return self.row_count > 0 and self.non_null_count == 0

    @property
    def dominant_shape(self) -> ShapeBucket | None:
        return self.shapes[0] if self.shapes else None

    @property
    def shape_coverage(self) -> float | None:
        """Share of non-null values explained by the recorded shapes.

        High coverage means the column has a dependable format worth pinning in
        a contract; low coverage means free text where a format rule would only
        generate noise.
        """
        if not self.shapes or not self.valued_count:
            return None
        return round(min(sum(s.count for s in self.shapes) / self.valued_count, 1.0), 6)

    def pattern_rate(self, key: str) -> float:
        total = self.valued_count
        return (self.pattern_hits.get(key, 0) / total) if total else 0.0

    @property
    def pii_signals(self) -> list[str]:
        from .patterns import PII_KEYS  # local import avoids a cycle

        return [k for k in self.pattern_hits if k in PII_KEYS and self.pattern_hits[k] > 0]

    def has(self, observation: Observation | str) -> bool:
        key = observation.value if isinstance(observation, Observation) else observation
        return key in self.observations

    # ── serialisation ─────────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return _prune(
            {
                "name": self.name,
                "position": self.position,
                "declared_type": self.declared_type,
                "inferred_type": self.inferred_type.value,
                "type_confidence": _round(self.type_confidence, 4),
                "type_mixture": {k: round(v, 4) for k, v in self.type_mixture.items()} or None,
                "type_probes": {k: v for k, v in self.type_probes.items() if v} or None,
                "row_count": self.row_count,
                "null_count": self.null_count,
                "null_rate": self.null_rate,
                "blank_count": self.blank_count or None,
                "distinct_count": self.distinct_count,
                "distinct_rate": self.distinct_rate,
                "distinct_is_approx": self.distinct_is_approx or None,
                "is_unique": self.is_unique or None,
                "is_constant": self.is_constant or None,
                "pattern_hits": {k: v for k, v in self.pattern_hits.items() if v} or None,
                "numeric": self.numeric.to_dict() if self.numeric else None,
                "text": self.text.to_dict() if self.text else None,
                "temporal": self.temporal.to_dict() if self.temporal else None,
                "shapes": [s.to_dict() for s in self.shapes] or None,
                "shape_distinct_count": self.shape_distinct_count,
                "shape_coverage": self.shape_coverage,
                "enum": self.enum.to_dict() if self.enum else None,
                "samples": list(self.samples) or None,
                "observations": list(self.observations) or None,
            }
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ColumnProfile:
        return cls(
            name=data["name"],
            position=int(data.get("position", 0)),
            declared_type=data.get("declared_type", "unknown"),
            inferred_type=InferredType(data.get("inferred_type", "text")),
            type_confidence=float(data.get("type_confidence", 0.0)),
            type_mixture=dict(data.get("type_mixture", {})),
            type_probes=dict(data.get("type_probes", {})),
            row_count=int(data.get("row_count", 0)),
            null_count=int(data.get("null_count", 0)),
            blank_count=int(data.get("blank_count", 0)),
            distinct_count=int(data.get("distinct_count", 0)),
            distinct_is_approx=bool(data.get("distinct_is_approx", False)),
            pattern_hits=dict(data.get("pattern_hits", {})),
            numeric=NumericStats.from_dict(data["numeric"]) if data.get("numeric") else None,
            text=TextStats.from_dict(data["text"]) if data.get("text") else None,
            temporal=TemporalStats.from_dict(data["temporal"]) if data.get("temporal") else None,
            shapes=[ShapeBucket.from_dict(s) for s in data.get("shapes", [])],
            shape_distinct_count=data.get("shape_distinct_count"),
            enum=EnumProfile.from_dict(data["enum"]) if data.get("enum") else None,
            samples=list(data.get("samples", [])),
            observations=list(data.get("observations", [])),
        )


@dataclass
class TableProfile:
    name: str
    source_uri: str = ""
    engine: str = ""
    dialect: str = ""

    row_count: int = 0
    columns: list[ColumnProfile] = field(default_factory=list)

    profiled_at: str = ""
    duration_ms: int = 0
    query_count: int = 0
    privacy_mode: str = "masked"

    zeyvor_version: str = ""
    schema_version: int = PROFILE_SCHEMA_VERSION
    warnings: list[str] = field(default_factory=list)

    @property
    def column_count(self) -> int:
        return len(self.columns)

    def column(self, name: str) -> ColumnProfile:
        for col in self.columns:
            if col.name == name:
                return col
        raise KeyError(f"No column named {name!r} in profile of {self.name!r}")

    def get(self, name: str) -> ColumnProfile | None:
        try:
            return self.column(name)
        except KeyError:
            return None

    def columns_with(self, observation: Observation | str) -> list[ColumnProfile]:
        return [c for c in self.columns if c.has(observation)]

    def fingerprint(self) -> str:
        """Digest of *structure*, deliberately blind to volume and timing.

        Re-profiling the same table twice with different row counts yields the
        same fingerprint; a renamed column, a changed inferred type, a new enum
        member or a new dominant format changes it. That makes it a cheap
        pre-check before running a full comparison.
        """
        import hashlib

        parts: list[str] = []
        for col in sorted(self.columns, key=lambda c: c.name):
            enum_part = ""
            if col.enum and col.enum.complete:
                enum_part = "|".join(sorted(col.enum.values()))
            shape_part = col.dominant_shape.shape if col.dominant_shape else ""
            parts.append(
                f"{col.name}:{col.inferred_type.value}:{int(col.is_unique)}"
                f":{shape_part}:{enum_part}"
            )
        digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
        return f"sha256:{digest[:16]}"

    def to_dict(self) -> dict[str, Any]:
        return _prune(
            {
                "schema_version": self.schema_version,
                "zeyvor_version": self.zeyvor_version,
                "name": self.name,
                "source_uri": self.source_uri,
                "engine": self.engine,
                "dialect": self.dialect,
                "privacy_mode": self.privacy_mode,
                "profiled_at": self.profiled_at,
                "duration_ms": self.duration_ms,
                "query_count": self.query_count,
                "row_count": self.row_count,
                "column_count": self.column_count,
                "fingerprint": self.fingerprint(),
                "warnings": list(self.warnings) or None,
                "columns": [c.to_dict() for c in self.columns],
            }
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TableProfile:
        return cls(
            name=data.get("name", ""),
            source_uri=data.get("source_uri", ""),
            engine=data.get("engine", ""),
            dialect=data.get("dialect", ""),
            row_count=int(data.get("row_count", 0)),
            columns=[ColumnProfile.from_dict(c) for c in data.get("columns", [])],
            profiled_at=data.get("profiled_at", ""),
            duration_ms=int(data.get("duration_ms", 0)),
            query_count=int(data.get("query_count", 0)),
            privacy_mode=data.get("privacy_mode", "masked"),
            zeyvor_version=data.get("zeyvor_version", ""),
            schema_version=int(data.get("schema_version", PROFILE_SCHEMA_VERSION)),
            warnings=list(data.get("warnings", [])),
        )

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> TableProfile:
        return cls.from_dict(json.loads(raw))

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(self.to_json())

    @classmethod
    def load(cls, path: str) -> TableProfile:
        with open(path, encoding="utf-8") as handle:
            return cls.from_json(handle.read())


__all__ = [
    "PROFILE_SCHEMA_VERSION",
    "InferredType",
    "Observation",
    "NumericStats",
    "TextStats",
    "TemporalStats",
    "ShapeBucket",
    "EnumMember",
    "EnumProfile",
    "ColumnProfile",
    "TableProfile",
]
