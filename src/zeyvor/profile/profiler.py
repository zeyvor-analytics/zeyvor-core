"""The Profiler: turn a relation into a TableProfile in a handful of queries."""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from ..engines.base import Engine, EngineError, Relation
from . import sql as sqlgen
from .models import (
    ColumnProfile,
    EnumMember,
    EnumProfile,
    NumericStats,
    ShapeBucket,
    TableProfile,
    TemporalStats,
    TextStats,
)
from .patterns import ALL_PATTERNS, Pattern, patterns_for
from .privacy import PrivacyMode, apply_to_enum_value, coerce_mode, sample_limit
from .types import DEFAULT_THRESHOLDS, Thresholds, finalise


@dataclass
class ProfileOptions:
    privacy: PrivacyMode | str = PrivacyMode.MASKED

    max_enum_cardinality: int = 50
    """Columns with at most this many distinct values get their full category
    set recorded. Above it, a contract could not meaningfully assert membership
    and the value set stops being business vocabulary."""

    enum_member_limit: int = 64
    shape_limit: int = 12

    column_batch_size: int = 20
    """Columns per query. Each column contributes ~50 aggregate expressions, so
    this trades planning cost against round trips. Wide tables stay fast either
    way; warehouses prefer smaller batches."""

    exact_distinct: bool | None = None
    include_quantiles: bool = True
    patterns: Sequence[str] | None = None
    thresholds: Thresholds = DEFAULT_THRESHOLDS
    samples: int | None = None

    def resolved_patterns(self) -> tuple[Pattern, ...]:
        return ALL_PATTERNS if self.patterns is None else patterns_for(list(self.patterns))


class Profiler:
    def __init__(self, engine: Engine, options: ProfileOptions | None = None) -> None:
        self.engine = engine
        self.options = options or ProfileOptions()
        self.privacy = coerce_mode(self.options.privacy)

    # ── public API ────────────────────────────────────────────────────────────

    def profile(self, relation: Relation) -> TableProfile:
        started = time.perf_counter()
        dialect = self.engine.dialect
        warnings: list[str] = []
        queries_before = self.engine.query_count

        names, declared_types = self._describe(relation, warnings)
        if not names:
            raise EngineError(f"No columns found in {relation.name!r}")

        row_count = int(self.engine.execute_one(sqlgen.row_count_sql(relation))[0] or 0)

        columns = [
            ColumnProfile(
                name=name,
                position=index,
                declared_type=declared_types.get(name, "unknown"),
                row_count=row_count,
            )
            for index, name in enumerate(names)
        ]

        if row_count > 0:
            patterns = self.options.resolved_patterns()
            exact = (
                dialect.exact_distinct_default
                if self.options.exact_distinct is None
                else self.options.exact_distinct
            )
            self._measure_scalars(relation, columns, patterns, exact_distinct=exact)
            self._measure_shapes(relation, columns)
            self._measure_enums(relation, columns)
            if sample_limit(self.privacy) > 0:
                self._measure_samples(relation, columns)

        for column in columns:
            finalise(column, self.options.thresholds)

        duration_ms = int((time.perf_counter() - started) * 1000)
        return TableProfile(
            name=relation.name,
            source_uri=relation.source_uri,
            engine=type(self.engine).__name__,
            dialect=dialect.name,
            row_count=row_count,
            columns=columns,
            profiled_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            duration_ms=duration_ms,
            query_count=self.engine.query_count - queries_before,
            privacy_mode=self.privacy.value,
            zeyvor_version=_version(),
            warnings=warnings,
        )

    # ── description ───────────────────────────────────────────────────────────

    def _describe(self, relation: Relation, warnings: list[str]) -> tuple[list[str], dict[str, str]]:
        """Column names from the text relation; declared types from the typed one.

        Files are read as all-VARCHAR so that profiling cannot be broken by bad
        values, which means the declared types have to come from a second,
        normally-typed read. When that read fails, the failure is itself worth
        recording: it means the file does not conform to the types its own
        contents imply.
        """
        names = [name for name, _ in self.engine.columns(relation)]

        declared: dict[str, str] = {}
        if relation.typed_sql:
            typed_relation = replace(relation, sql=relation.typed_sql, typed_sql=None)
            try:
                declared = {name: dtype for name, dtype in self.engine.columns(typed_relation)}
            except EngineError as exc:
                warnings.append(
                    "Could not read the source with its own inferred types "
                    f"({_short(exc)}). Declared types are unavailable; profiling "
                    "continued on the text representation."
                )
        else:
            declared = {name: dtype for name, dtype in self.engine.columns(relation)}

        return names, declared

    # ── pass 2 ────────────────────────────────────────────────────────────────

    def _measure_scalars(
        self,
        relation: Relation,
        columns: list[ColumnProfile],
        patterns: tuple[Pattern, ...],
        *,
        exact_distinct: bool,
    ) -> None:
        for batch in _batched(columns, self.options.column_batch_size):
            statement, layouts = sqlgen.scalar_stats_sql(
                self.engine.dialect,
                relation,
                [c.name for c in batch],
                patterns=patterns,
                exact_distinct=exact_distinct,
                include_quantiles=self.options.include_quantiles,
            )
            row = self.engine.execute_one(statement)
            cursor = 0
            for column, layout in zip(batch, layouts):
                values = dict(zip(layout.metrics, row[cursor : cursor + len(layout.metrics)]))
                cursor += len(layout.metrics)
                self._apply_scalars(column, values, exact_distinct=exact_distinct)

    def _apply_scalars(
        self, column: ColumnProfile, values: dict[str, Any], *, exact_distinct: bool
    ) -> None:
        non_null = _int(values.get("nonnull"))
        column.null_count = max(column.row_count - non_null, 0)
        column.blank_count = _int(values.get("blank"))
        column.distinct_count = _int(values.get("distinct"))
        column.distinct_is_approx = not exact_distinct
        column.shape_distinct_count = _int(values.get("shape_distinct")) or None

        column.type_probes = {
            key: _int(values.get(f"probe_{key}"))
            for key in ("int", "float", "bool", "date", "timestamp")
        }
        column.pattern_hits = {
            key[4:]: _int(count)
            for key, count in values.items()
            if key.startswith("pat_") and _int(count) > 0
        }

        valued = column.valued_count
        int_share = (column.type_probes.get("int", 0) / valued) if valued else 0.0
        float_share = (column.type_probes.get("float", 0) / valued) if valued else 0.0
        date_share = (column.type_probes.get("date", 0) / valued) if valued else 0.0
        ts_share = (column.type_probes.get("timestamp", 0) / valued) if valued else 0.0

        if float_share > 0:
            column.numeric = NumericStats(
                minimum=_float(values.get("num_min")),
                maximum=_float(values.get("num_max")),
                mean=_float(values.get("num_mean")),
                stddev=_float(values.get("num_stddev")),
                p25=_float(values.get("num_p25")),
                p50=_float(values.get("num_p50")),
                p75=_float(values.get("num_p75")),
                zero_count=_int(values.get("num_zero")),
                negative_count=_int(values.get("num_negative")),
                integral_count=_int(values.get("num_integral")),
                parseable_count=column.type_probes.get("float", 0),
            )

        if valued:
            column.text = TextStats(
                min_length=_int_or_none(values.get("len_min")),
                max_length=_int_or_none(values.get("len_max")),
                mean_length=_float(values.get("len_mean")),
                whitespace_padded_count=_int(values.get("ws_padded")),
                uppercase_count=_int(values.get("case_upper")),
                lowercase_count=_int(values.get("case_lower")),
                mixed_case_count=_int(values.get("case_mixed")),
            )

        # Guard against a column of plain integers being reported as a date
        # range: some engines will happily cast a digit string to a timestamp.
        temporal_present = max(date_share, ts_share) >= 0.01
        if temporal_present and int_share < 0.99:
            column.temporal = TemporalStats(
                minimum=_str_or_none(values.get("ts_min")),
                maximum=_str_or_none(values.get("ts_max")),
                future_count=_int(values.get("ts_future")),
                distant_past_count=_int(values.get("ts_distant_past")),
                parseable_count=max(
                    column.type_probes.get("date", 0), column.type_probes.get("timestamp", 0)
                ),
            )

    # ── pass 3 ────────────────────────────────────────────────────────────────

    def _measure_shapes(self, relation: Relation, columns: list[ColumnProfile]) -> None:
        candidates = [(c.position, c.name) for c in columns if c.valued_count > 0]
        if not candidates:
            return
        by_index = {c.position: c for c in columns}
        for batch in _batched(candidates, self.options.column_batch_size):
            statement = sqlgen.shape_sql(
                self.engine.dialect, relation, list(batch), limit=self.options.shape_limit
            )
            for index, bucket, count in self.engine.execute(statement):
                column = by_index.get(int(index))
                if column is None:
                    continue
                total = column.valued_count
                column.shapes.append(
                    ShapeBucket(
                        shape=_str_or_none(bucket) or "",
                        count=_int(count),
                        rate=round(_int(count) / total, 6) if total else None,
                    )
                )
        for column in columns:
            column.shapes.sort(key=lambda s: (-s.count, s.shape))

    # ── pass 4 ────────────────────────────────────────────────────────────────

    def _measure_enums(self, relation: Relation, columns: list[ColumnProfile]) -> None:
        # A column whose every value is distinct is not a category set — it is
        # the data itself. Recording it would produce a useless "enum" of
        # customer names and, worse, would dump those values into an artefact
        # meant to be safe to share.
        candidates = [
            (c.position, c.name)
            for c in columns
            if 0 < c.distinct_count <= self.options.max_enum_cardinality
            and not c.distinct_is_approx
            and not c.is_unique
        ]
        if not candidates:
            return
        by_index = {c.position: c for c in columns}
        limit = max(self.options.enum_member_limit, self.options.max_enum_cardinality)
        for batch in _batched(candidates, self.options.column_batch_size):
            statement = sqlgen.enum_sql(self.engine.dialect, relation, list(batch), limit=limit)
            for index, bucket, count in self.engine.execute(statement):
                column = by_index.get(int(index))
                if column is None:
                    continue
                if column.enum is None:
                    column.enum = EnumProfile(cardinality=column.distinct_count)
                total = column.non_null_count
                column.enum.members.append(
                    EnumMember(
                        value=apply_to_enum_value(_str_or_none(bucket) or "", self.privacy),
                        count=_int(count),
                        rate=round(_int(count) / total, 6) if total else None,
                    )
                )

        for column in columns:
            if column.enum is None:
                continue
            column.enum.members.sort(key=lambda m: (-m.count, m.value))
            column.enum.hashed = self.privacy is PrivacyMode.STRICT
            # A set is only "complete" — and therefore only usable to detect a
            # new category later — if nothing was truncated by the limit.
            column.enum.complete = len(column.enum.members) >= column.distinct_count

    # ── pass 5 ────────────────────────────────────────────────────────────────

    def _measure_samples(self, relation: Relation, columns: list[ColumnProfile]) -> None:
        limit = self.options.samples or sample_limit(self.privacy)
        if limit <= 0:
            return
        candidates = [(c.position, c.name) for c in columns if c.valued_count > 0]
        if not candidates:
            return
        by_index = {c.position: c for c in columns}
        for batch in _batched(candidates, self.options.column_batch_size):
            statement = sqlgen.sample_sql(self.engine.dialect, relation, list(batch), limit=limit)
            for index, value in self.engine.execute(statement):
                column = by_index.get(int(index))
                if column is not None and len(column.samples) < limit:
                    column.samples.append(_str_or_none(value) or "")


# ── convenience entry point ───────────────────────────────────────────────────


def profile_source(
    source: str,
    *,
    table: str | None = None,
    options: ProfileOptions | None = None,
    engine: Engine | None = None,
    **resolve_kwargs: Any,
) -> TableProfile:
    """Profile anything addressable by a source string.

    >>> profile = profile_source("orders.csv")
    >>> profile.column("signup_date").inferred_type
    <InferredType.DATE: 'date'>
    """
    from ..sources import resolve_source  # local import avoids a cycle

    resolved = resolve_source(source, table=table, engine=engine, **resolve_kwargs)
    try:
        return Profiler(resolved.engine, options).profile(resolved.relation)
    finally:
        resolved.close()


# ── small helpers ─────────────────────────────────────────────────────────────


def _batched(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    size = max(int(size), 1)
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _int(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result in (float("inf"), float("-inf")):  # NaN / Inf
        return None
    return result


def _str_or_none(value: Any) -> str | None:
    return None if value is None else str(value)


def _short(exc: Exception, limit: int = 120) -> str:
    text = " ".join(str(exc).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _version() -> str:
    from .. import __version__  # local import avoids a cycle

    return __version__


__all__ = ["ProfileOptions", "Profiler", "profile_source"]
