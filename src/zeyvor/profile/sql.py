"""SQL generation: every measurement, expressed as an aggregate.

The design goal is a fixed, small number of round trips regardless of how wide
the table is. A 200-column table is profiled in the same handful of queries as a
5-column one, because all per-column metrics for a batch are computed as
separate expressions inside a *single* SELECT:

    pass 1   row count
    pass 2   every scalar metric for every column        (batched, N/batch_size queries)
    pass 3   value-shape histograms                      (one UNION ALL query per batch)
    pass 4   category members for low-cardinality columns (one UNION ALL query per batch)
    pass 5   sample values                                (FULL privacy mode only)

Two expression forms recur throughout:

``t``  the column as text — the null/blank-aware base for counting.
``v``  ``t`` trimmed, with blanks turned into NULL — the base for every type
       probe, so that ``" 42 "`` still reads as an integer while padding is
       recorded separately as a hygiene finding.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..engines.base import Dialect, Relation
from .patterns import Pattern, shape_expression
from .types import PROBE_BOOL, PROBE_DATE, PROBE_FLOAT, PROBE_INT, PROBE_TIMESTAMP

DISTANT_PAST = "1900-01-01 00:00:00"


@dataclass
class ColumnLayout:
    """Which metrics a column contributed to the SELECT list, in order."""

    name: str
    metrics: list[str] = field(default_factory=list)


def text_expr(dialect: Dialect, column: str) -> str:
    return dialect.as_text(dialect.quote_ident(column))


def valued_expr(dialect: Dialect, column: str) -> str:
    """Trimmed text, with empty strings promoted to NULL."""
    t = text_expr(dialect, column)
    trimmed = dialect.trim(t)
    return f"NULLIF({trimmed}, '')"


# ── pass 1: row count ─────────────────────────────────────────────────────────


def row_count_sql(relation: Relation) -> str:
    return f"SELECT COUNT(*) FROM {relation.sql}"


# ── pass 2: scalar metrics ────────────────────────────────────────────────────


def scalar_stats_sql(
    dialect: Dialect,
    relation: Relation,
    columns: list[str],
    *,
    patterns: tuple[Pattern, ...],
    exact_distinct: bool = True,
    include_quantiles: bool = True,
) -> tuple[str, list[ColumnLayout]]:
    """One SELECT computing every scalar metric for every column in the batch."""
    select_parts: list[str] = []
    layouts: list[ColumnLayout] = []

    for index, column in enumerate(columns):
        layout = ColumnLayout(name=column)
        quoted = dialect.quote_ident(column)
        t = text_expr(dialect, column)
        v = valued_expr(dialect, column)
        num = dialect.try_cast(v, dialect.float_type)
        ts = dialect.try_cast(v, dialect.timestamp_type)

        def add(metric: str, expression: str, _layout: ColumnLayout = layout, _i: int = index) -> None:
            select_parts.append(f"{expression} AS c{_i}__{metric}")
            _layout.metrics.append(metric)

        # ── presence and cardinality ──────────────────────────────────────────
        add("nonnull", f"COUNT({quoted})")
        add("blank", dialect.sum_case(f"{quoted} IS NOT NULL AND {dialect.trim(t)} = ''"))
        distinct_fn = dialect.count_distinct if exact_distinct else dialect.approx_count_distinct
        add("distinct", distinct_fn(t))

        # ── type probes ───────────────────────────────────────────────────────
        # The integer probe cannot simply be TRY_CAST(... AS BIGINT): DuckDB
        # *rounds* '19.99' to 20 rather than failing, which would report every
        # float column as an integer one. Integrality is therefore established
        # from the value (no fractional part) and the representation (no decimal
        # point or exponent), which also keeps '20.0' correctly classed as a
        # float that happens to hold a whole number.
        add(
            "probe_" + PROBE_INT,
            dialect.sum_case(
                f"{num} IS NOT NULL AND {num} = {dialect.floor(num)} "
                f"AND NOT {dialect.regex_match(v, '[.eE]')}"
            ),
        )
        add("probe_" + PROBE_FLOAT, dialect.sum_case(f"{num} IS NOT NULL"))
        bool_vocab = "('true','false','t','f','yes','no','y','n','0','1')"
        add("probe_" + PROBE_BOOL, dialect.sum_case(f"{dialect.lower(v)} IN {bool_vocab}"))
        add("probe_" + PROBE_DATE, dialect.sum_case(f"{dialect.try_cast(v, dialect.date_type)} IS NOT NULL"))
        add("probe_" + PROBE_TIMESTAMP, dialect.sum_case(f"{ts} IS NOT NULL"))

        # ── pattern counts ────────────────────────────────────────────────────
        for pattern in patterns:
            subject = dialect.lower(v) if pattern.lowercase else v
            add(f"pat_{pattern.key}", dialect.sum_case(dialect.regex_match(subject, pattern.regex)))

        # ── numeric distribution ──────────────────────────────────────────────
        add("num_min", f"MIN({num})")
        add("num_max", f"MAX({num})")
        add("num_mean", f"AVG({num})")
        add("num_stddev", dialect.stddev(num))
        if include_quantiles:
            add("num_p25", dialect.quantile(num, 0.25))
            add("num_p50", dialect.quantile(num, 0.50))
            add("num_p75", dialect.quantile(num, 0.75))
        add("num_zero", dialect.sum_case(f"{num} = 0"))
        add("num_negative", dialect.sum_case(f"{num} < 0"))
        add("num_integral", dialect.sum_case(f"{num} IS NOT NULL AND {num} = {dialect.floor(num)}"))

        # ── text shape and hygiene ────────────────────────────────────────────
        length = dialect.length(v)
        add("len_min", f"MIN({length})")
        add("len_max", f"MAX({length})")
        add("len_mean", f"AVG({length})")
        add("ws_padded", dialect.sum_case(f"{t} IS NOT NULL AND {t} <> {dialect.trim(t)}"))
        upper, lower = dialect.upper(v), dialect.lower(v)
        add("case_upper", dialect.sum_case(f"{v} = {upper} AND {v} <> {lower}"))
        add("case_lower", dialect.sum_case(f"{v} = {lower} AND {v} <> {upper}"))
        add("case_mixed", dialect.sum_case(f"{v} <> {upper} AND {v} <> {lower}"))

        # ── temporal range ────────────────────────────────────────────────────
        add("ts_min", dialect.as_text(f"MIN({ts})"))
        add("ts_max", dialect.as_text(f"MAX({ts})"))
        add("ts_future", dialect.sum_case(f"{ts} > {dialect.current_timestamp()}"))
        add(
            "ts_distant_past",
            dialect.sum_case(f"{ts} < {dialect.timestamp_literal(DISTANT_PAST)}"),
        )

        # ── formatting diversity ──────────────────────────────────────────────
        add("shape_distinct", dialect.count_distinct(shape_expression(dialect, v)))

        layouts.append(layout)

    sql = "SELECT\n  " + ",\n  ".join(select_parts) + f"\nFROM {relation.sql}"
    return sql, layouts


# ── pass 3: shape histograms ──────────────────────────────────────────────────


def shape_sql(
    dialect: Dialect,
    relation: Relation,
    columns: list[tuple[int, str]],
    *,
    limit: int = 12,
) -> str:
    """UNION ALL of per-column shape histograms.

    Each branch returns ``(column_index, shape, count)`` so one round trip
    covers the whole batch.
    """
    branches: list[str] = []
    for index, column in columns:
        v = valued_expr(dialect, column)
        shape = shape_expression(dialect, v)
        branches.append(
            f"SELECT * FROM ("
            f"SELECT {index} AS col_idx, {shape} AS bucket, COUNT(*) AS bucket_count "
            f"FROM {relation.sql} WHERE {v} IS NOT NULL "
            f"GROUP BY 2 ORDER BY bucket_count DESC, bucket LIMIT {int(limit)})"
        )
    return "\nUNION ALL\n".join(branches)


# ── pass 4: category members ──────────────────────────────────────────────────


def enum_sql(
    dialect: Dialect,
    relation: Relation,
    columns: list[tuple[int, str]],
    *,
    limit: int = 64,
) -> str:
    """UNION ALL of per-column value histograms for low-cardinality columns."""
    branches: list[str] = []
    for index, column in columns:
        t = text_expr(dialect, column)
        branches.append(
            f"SELECT * FROM ("
            f"SELECT {index} AS col_idx, {t} AS bucket, COUNT(*) AS bucket_count "
            f"FROM {relation.sql} WHERE {dialect.quote_ident(column)} IS NOT NULL "
            f"GROUP BY 2 ORDER BY bucket_count DESC, bucket LIMIT {int(limit)})"
        )
    return "\nUNION ALL\n".join(branches)


# ── pass 5: samples (FULL privacy mode only) ──────────────────────────────────


def sample_sql(
    dialect: Dialect,
    relation: Relation,
    columns: list[tuple[int, str]],
    *,
    limit: int = 5,
) -> str:
    branches: list[str] = []
    for index, column in columns:
        v = valued_expr(dialect, column)
        branches.append(
            f"SELECT * FROM ("
            f"SELECT {index} AS col_idx, {v} AS value FROM {relation.sql} "
            f"WHERE {v} IS NOT NULL LIMIT {int(limit)})"
        )
    return "\nUNION ALL\n".join(branches)


__all__ = [
    "ColumnLayout",
    "row_count_sql",
    "scalar_stats_sql",
    "shape_sql",
    "enum_sql",
    "sample_sql",
    "text_expr",
    "valued_expr",
]
