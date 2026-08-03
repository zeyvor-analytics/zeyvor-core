"""Type inference and observation derivation from aggregate counts.

The interesting constraint here is that inference happens *without values*. All
this module ever sees is "9,700 of 10,000 values cast to DATE, 300 cast to
BIGINT, 300 match the epoch-seconds shape" — and from that it concludes the
column is a date column that has started receiving Unix timestamps.

Working from counts rather than rows is what makes profiling a single SQL pass
instead of a download, and it is also what makes the output safe to share.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import ColumnProfile, InferredType, Observation

# Probe keys produced by the SQL layer.
PROBE_INT = "int"
PROBE_FLOAT = "float"
PROBE_BOOL = "bool"
PROBE_DATE = "date"
PROBE_TIMESTAMP = "timestamp"

# Type families that describe the same column rather than a disagreement. Used
# both for the observed mixture within a column and for declared-vs-inferred, so
# the two cannot drift apart. Note these are family names, as carried by
# `type_mixture` and `declared_family`, not the probe keys above.
COMPATIBLE_TYPE_FAMILIES = (
    {"integer", "float"},
    {"date", "timestamp"},
)

TEXT_TYPE_TOKENS = ("char", "text", "string", "varchar", "clob", "object", "json")
INT_TYPE_TOKENS = ("int", "serial", "number", "numeric", "decimal", "bigint")
FLOAT_TYPE_TOKENS = ("float", "double", "real", "numeric", "decimal")
DATE_TYPE_TOKENS = ("date",)
TIMESTAMP_TYPE_TOKENS = ("timestamp", "datetime")
BOOL_TYPE_TOKENS = ("bool",)


@dataclass(frozen=True)
class Thresholds:
    """Tunable decision boundaries.

    Defaults lean conservative: a type is only declared when essentially every
    value agrees, because a false "this column is a date" in a generated
    contract turns into a false alarm on every subsequent run, and a checker
    that cries wolf gets uninstalled.
    """

    type_dominant: float = 0.99
    """Share of non-null values that must agree before a type is asserted."""

    type_mixed_floor: float = 0.70
    """Combined structured share above which a column is MIXED rather than TEXT."""

    minority_type: float = 0.001
    """Smallest minority share worth reporting as mixed types. One row in a
    thousand is enough — that is usually the leading edge of a breaking change."""

    pattern_dominant: float = 0.90
    pattern_signal: float = 0.10
    pattern_trace: float = 0.01

    high_null_rate: float = 0.50
    case_signal: float = 0.05
    date_format_signal: float = 0.05
    pii_min_count: int = 1


DEFAULT_THRESHOLDS = Thresholds()


def declared_family(declared_type: str) -> str:
    """Collapse an engine-specific type name into a coarse family."""
    lowered = (declared_type or "").lower()
    if not lowered or lowered == "unknown":
        return "unknown"
    if any(token in lowered for token in BOOL_TYPE_TOKENS):
        return "boolean"
    if any(token in lowered for token in TIMESTAMP_TYPE_TOKENS):
        return "timestamp"
    if any(token in lowered for token in DATE_TYPE_TOKENS):
        return "date"
    if any(token in lowered for token in TEXT_TYPE_TOKENS):
        return "text"
    # Float tokens are checked before int tokens because NUMERIC/DECIMAL appear
    # in both lists and are more usefully treated as floats.
    if any(token in lowered for token in ("float", "double", "real")):
        return "float"
    if any(token in lowered for token in ("decimal", "numeric")):
        return "float"
    if any(token in lowered for token in INT_TYPE_TOKENS):
        return "integer"
    return "other"


def inferred_family(inferred: InferredType) -> str:
    return {
        InferredType.INTEGER: "integer",
        InferredType.FLOAT: "float",
        InferredType.BOOLEAN: "boolean",
        InferredType.DATE: "date",
        InferredType.TIMESTAMP: "timestamp",
    }.get(inferred, "text")


def _share(count: int, total: int) -> float:
    return (count / total) if total else 0.0


def infer_type(
    column: ColumnProfile,
    thresholds: Thresholds = DEFAULT_THRESHOLDS,
) -> tuple[InferredType, float, dict[str, float]]:
    """Return ``(inferred_type, confidence, approximate_mixture)``."""
    total = column.valued_count
    if total == 0:
        return InferredType.EMPTY, 1.0, {}

    probes = column.type_probes
    s_int = _share(probes.get(PROBE_INT, 0), total)
    s_float = _share(probes.get(PROBE_FLOAT, 0), total)
    s_bool = _share(probes.get(PROBE_BOOL, 0), total)
    s_date = _share(probes.get(PROBE_DATE, 0), total)
    s_ts = _share(probes.get(PROBE_TIMESTAMP, 0), total)

    p = column.pattern_rate
    mixture = _mixture(column, total)

    # Structured text formats win outright when near-unanimous: a UUID column is
    # more usefully described as a UUID than as text.
    for pattern_key, kind in (
        ("uuid", InferredType.UUID),
        ("email", InferredType.EMAIL),
        ("url", InferredType.URL),
        ("json_object", InferredType.JSON),
    ):
        if p(pattern_key) >= thresholds.pattern_dominant:
            return kind, round(p(pattern_key), 4), mixture

    # Booleans before integers: '0'/'1' satisfy both probes, so cardinality is
    # the tie-breaker.
    if s_bool >= thresholds.type_dominant and column.distinct_count <= 3:
        return InferredType.BOOLEAN, round(s_bool, 4), mixture

    # Integers before floats: every integer also casts to DOUBLE.
    if s_int >= thresholds.type_dominant:
        return InferredType.INTEGER, round(s_int, 4), mixture

    if s_float >= thresholds.type_dominant:
        return InferredType.FLOAT, round(s_float, 4), mixture

    # Dates before timestamps, since a timestamp usually casts to DATE too. The
    # presence of a time component in the *shape* decides which is meant.
    #
    # Cast probes alone are not enough here: DuckDB parses ISO dates but refuses
    # '3/11/2024' and '11.03.2024', so a column of perfectly good US- or
    # EU-formatted dates would be dismissed as text. Format evidence fills the
    # gap, and the recorded shape lets Part 2 pin the expected format.
    date_evidence = max(s_date, _format_date_share(column))
    if date_evidence >= thresholds.type_dominant:
        if p("iso_datetime") >= 0.5:
            return InferredType.TIMESTAMP, round(max(s_ts, date_evidence), 4), mixture
        return InferredType.DATE, round(date_evidence, 4), mixture

    if s_ts >= thresholds.type_dominant:
        return InferredType.TIMESTAMP, round(s_ts, 4), mixture

    structured = max(s_int, s_float, s_date, s_ts, s_bool)
    combined = sum(v for k, v in mixture.items() if k != "text")
    if combined >= thresholds.type_mixed_floor and structured < thresholds.type_dominant:
        return InferredType.MIXED, round(combined, 4), mixture

    # Text is the residual: confidence expresses how confidently the column is
    # *not* something more specific.
    return InferredType.TEXT, round(max(0.0, 1.0 - structured), 4), mixture


def _format_date_share(column: ColumnProfile) -> float:
    """Share of values that *look* like calendar dates in any common format.

    The three formats are mutually exclusive by construction, so summing them is
    safe and lets a column of mixed ISO/US dates still read as temporal.
    """
    p = column.pattern_rate
    return min(p("iso_date") + p("us_date") + p("eu_date"), 1.0)


def _format_date_count(column: ColumnProfile) -> int:
    hits = column.pattern_hits
    return hits.get("iso_date", 0) + hits.get("us_date", 0) + hits.get("eu_date", 0)


def _mixture(column: ColumnProfile, total: int) -> dict[str, float]:
    """Approximate disjoint classification of non-null values.

    Cast probes overlap — '5' satisfies BIGINT, DOUBLE and BOOLEAN-ish tests —
    so exact partitioning is impossible from counts alone. Subtracting nested
    probes gets close enough to be useful, and being explicit about the
    approximation is better than pretending otherwise. The case that matters is
    faithfully represented: 97% date / 3% integer shows up as exactly that.
    """
    probes = column.type_probes
    n_int = probes.get(PROBE_INT, 0)
    n_float = probes.get(PROBE_FLOAT, 0)
    # Pattern-detected dates count too, otherwise a US-formatted date column
    # would appear as 100% text in the mixture.
    n_date = max(probes.get(PROBE_DATE, 0), _format_date_count(column))
    n_ts = probes.get(PROBE_TIMESTAMP, 0)

    integer = n_int
    float_only = max(n_float - n_int, 0)
    date = min(n_date, max(total - integer - float_only, 0))
    timestamp_only = max(n_ts - n_date, 0)
    accounted = integer + float_only + date + timestamp_only
    text_rest = max(total - accounted, 0)

    raw = {
        "integer": integer,
        "float": float_only,
        "date": date,
        "timestamp": timestamp_only,
        "text": text_rest,
    }
    return {k: round(v / total, 6) for k, v in raw.items() if v > 0 and total}


def derive_observations(
    column: ColumnProfile,
    thresholds: Thresholds = DEFAULT_THRESHOLDS,
) -> list[str]:
    """Facts a contract can be written against, in stable key form."""
    out: list[str] = []
    add = out.append
    p = column.pattern_rate
    hits = column.pattern_hits

    if column.is_empty:
        add(Observation.EMPTY_COLUMN.value)
        return out

    if column.is_constant:
        add(Observation.CONSTANT.value)
    if column.is_unique:
        add(Observation.UNIQUE.value)
    if (column.null_rate or 0) >= thresholds.high_null_rate:
        add(Observation.HIGH_NULL_RATE.value)

    # ── mixed types ───────────────────────────────────────────────────────────
    mixture = column.type_mixture
    if mixture:
        ranked = sorted(mixture.items(), key=lambda kv: kv[1], reverse=True)
        # int alongside float is not a mixture worth reporting: it is what every
        # float column looks like the moment one value lands on a whole number.
        # A price column where 4 in 10 rows end in .00 is ordinary, and at the
        # 0.001 minority threshold a single such value was enough to flag the
        # column. The declared-vs-inferred check below already treats this pair
        # as compatible; the two now agree.
        compatible = len(ranked) >= 2 and {ranked[0][0], ranked[1][0]} in COMPATIBLE_TYPE_FAMILIES
        if len(ranked) >= 2 and ranked[1][1] >= thresholds.minority_type and not compatible:
            add(Observation.MIXED_TYPES.value)
    if column.inferred_type is InferredType.MIXED and Observation.MIXED_TYPES.value not in out:
        add(Observation.MIXED_TYPES.value)

    # ── declared vs inferred ──────────────────────────────────────────────────
    declared = declared_family(column.declared_type)
    inferred = inferred_family(column.inferred_type)
    if declared == "text" and inferred in ("integer", "float"):
        add(Observation.NUMERIC_STORED_AS_TEXT.value)
    if declared == "text" and inferred in ("date", "timestamp"):
        add(Observation.TEMPORAL_STORED_AS_TEXT.value)
    if declared == "text" and inferred == "boolean":
        add(Observation.BOOLEAN_STORED_AS_TEXT.value)

    # A flag column spelled six different ways — true/TRUE/yes/YES/1/t — is
    # semantically boolean but will not behave like one. Extremely common in
    # exported files, and "mixed types" badly under-describes it: the fix is to
    # normalise a vocabulary, not to change a type.
    bool_share = _share(column.type_probes.get(PROBE_BOOL, 0), column.valued_count)
    # An all-numeric column is a code, not a vocabulary. `0` and `1` both parse
    # as boolean, so any small integer encoding where most rows happen to be 0 or
    # 1 cleared the share test on arithmetic alone — a three-valued ordinal like
    # slope={0,1,2} at 93% zeros-and-ones was reported as a boolean spelled
    # inconsistently. The failure this observation exists to describe needs more
    # than one *spelling*, which means values that are not all integers.
    int_share = _share(column.type_probes.get(PROBE_INT, 0), column.valued_count)
    if (
        bool_share >= thresholds.pattern_dominant
        and int_share < thresholds.pattern_dominant
        and column.distinct_count > 2
        and column.inferred_type is not InferredType.BOOLEAN
    ):
        add(Observation.MIXED_BOOLEAN_ENCODING.value)
    if (
        declared not in ("unknown", "other", "text")
        and inferred != "text"
        and declared != inferred
        and {declared, inferred} not in COMPATIBLE_TYPE_FAMILIES
    ):
        add(Observation.DECLARED_TYPE_CONFLICT.value)

    # ── temporal impostors ────────────────────────────────────────────────────
    # Context decides the threshold. A handful of epoch timestamps inside a
    # column that is otherwise dates is the single most valuable finding this
    # tool produces — it is a breaking upstream change caught on its first day,
    # and three rows in a thousand is plenty of evidence. The same shape in a
    # column that is *entirely* 10-digit integers is far weaker evidence, since
    # plenty of identifiers look like that, so it takes near-unanimity.
    epoch_share = max(p("epoch_seconds"), p("epoch_millis"))
    temporal_share = max(p("iso_date"), p("iso_datetime"), p("us_date"), p("eu_date"))
    if epoch_share > 0 and (
        temporal_share >= thresholds.pattern_trace or epoch_share >= thresholds.pattern_dominant
    ):
        add(Observation.EPOCH_SUSPECTED.value)

    # Excel serials are ambiguous with ordinary five-digit numbers, so they are
    # only reported alongside a temporal signal or when they account for the
    # whole column.
    excel_share = p("excel_serial")
    if (
        excel_share > 0
        and column.inferred_type is InferredType.INTEGER
        and (
            temporal_share >= thresholds.pattern_trace or excel_share >= thresholds.pattern_dominant
        )
    ):
        add(Observation.EXCEL_SERIAL_SUSPECTED.value)

    date_formats = [
        key
        for key in ("iso_date", "iso_datetime", "us_date", "eu_date")
        if p(key) >= thresholds.date_format_signal
    ]
    if len(date_formats) >= 2:
        add(Observation.MULTIPLE_DATE_FORMATS.value)

    # ── numbers wearing costumes ──────────────────────────────────────────────
    if hits.get("leading_zeros", 0) > 0:
        add(Observation.LEADING_ZEROS.value)
    if p("currency") >= thresholds.pattern_signal:
        add(Observation.CURRENCY_IN_TEXT.value)
    if p("percent") >= thresholds.pattern_signal:
        add(Observation.PERCENT_IN_TEXT.value)
    if p("thousands_separated") >= thresholds.pattern_signal:
        add(Observation.THOUSANDS_SEPARATORS.value)

    # ── hygiene ───────────────────────────────────────────────────────────────
    if hits.get("null_word", 0) > 0:
        add(Observation.NULL_WORDS.value)
    if hits.get("mojibake", 0) > 0:
        add(Observation.MOJIBAKE.value)
    if column.text and column.text.whitespace_padded_count > 0:
        add(Observation.WHITESPACE_PADDING.value)
    if column.text:
        case_counts = [
            column.text.uppercase_count,
            column.text.lowercase_count,
            column.text.mixed_case_count,
        ]
        # Only meaningful where most values actually contain letters. Without
        # this gate a date column sprinkled with "N/A" and "null" gets reported
        # for inconsistent capitalisation, which is true and useless.
        cased_share = _share(sum(case_counts), column.valued_count)
        if cased_share >= 0.5:
            significant = [
                c for c in case_counts if _share(c, column.valued_count) >= thresholds.case_signal
            ]
            if len(significant) >= 2:
                add(Observation.INCONSISTENT_CASE.value)

    # ── categories ────────────────────────────────────────────────────────────
    if column.enum and column.enum.complete and column.enum.cardinality > 1:
        add(Observation.ENUM_CANDIDATE.value)

    # ── PII ───────────────────────────────────────────────────────────────────
    from .patterns import PII_KEYS  # local import avoids a cycle

    pii_present = any(
        hits.get(key, 0) >= thresholds.pii_min_count and p(key) >= thresholds.pattern_trace
        for key in PII_KEYS
    )
    if pii_present:
        add(Observation.PII_DETECTED.value)

    # An email hiding inside a longer string is the leak a name-based checker
    # can never see: the column is called "notes", not "email".
    embedded = p("email_embedded")
    if embedded >= thresholds.pattern_trace and p("email") < 0.5:
        add(Observation.PII_IN_FREE_TEXT.value)

    return out


def finalise(column: ColumnProfile, thresholds: Thresholds = DEFAULT_THRESHOLDS) -> ColumnProfile:
    """Fill in inferred type, mixture and observations on a measured column."""
    inferred, confidence, mixture = infer_type(column, thresholds)
    column.inferred_type = inferred
    column.type_confidence = confidence
    column.type_mixture = mixture
    column.observations = derive_observations(column, thresholds)
    return column


__all__ = [
    "Thresholds",
    "DEFAULT_THRESHOLDS",
    "declared_family",
    "inferred_family",
    "infer_type",
    "derive_observations",
    "finalise",
    "PROBE_INT",
    "PROBE_FLOAT",
    "PROBE_BOOL",
    "PROBE_DATE",
    "PROBE_TIMESTAMP",
]
