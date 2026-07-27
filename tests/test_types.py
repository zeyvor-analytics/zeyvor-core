"""Inference tests with no engine involved.

These construct probe counts directly, which is the honest unit boundary: given
"9,700 values cast to DATE and 300 look like epoch seconds", what does Zeyvor
conclude? Fast, exact, and independent of any SQL behaviour.
"""

from __future__ import annotations

from zeyvor.profile.models import ColumnProfile, InferredType, Observation, TextStats
from zeyvor.profile.types import declared_family, finalise


def make_column(
    *,
    name: str = "col",
    rows: int = 100,
    nulls: int = 0,
    blanks: int = 0,
    distinct: int = 100,
    declared: str = "VARCHAR",
    probes: dict[str, int] | None = None,
    patterns: dict[str, int] | None = None,
    text: TextStats | None = None,
) -> ColumnProfile:
    column = ColumnProfile(
        name=name,
        declared_type=declared,
        row_count=rows,
        null_count=nulls,
        blank_count=blanks,
        distinct_count=distinct,
        type_probes=probes or {},
        pattern_hits=patterns or {},
        text=text,
    )
    return finalise(column)


# ── the type ladder ───────────────────────────────────────────────────────────


def test_integers():
    col = make_column(probes={"int": 100, "float": 100})
    assert col.inferred_type is InferredType.INTEGER
    assert col.type_confidence == 1.0


def test_floats_are_not_integers():
    """Values with a decimal point are floats even when integral in value."""
    col = make_column(probes={"int": 0, "float": 100})
    assert col.inferred_type is InferredType.FLOAT


def test_iso_dates():
    col = make_column(probes={"date": 100, "timestamp": 100}, patterns={"iso_date": 100})
    assert col.inferred_type is InferredType.DATE


def test_timestamps_beat_dates_when_a_time_is_present():
    col = make_column(
        probes={"date": 100, "timestamp": 100},
        patterns={"iso_datetime": 100},
    )
    assert col.inferred_type is InferredType.TIMESTAMP


def test_us_dates_infer_as_dates_without_a_cast():
    """No engine will cast '3/11/2024'; format evidence has to carry it."""
    col = make_column(probes={}, patterns={"us_date": 100})
    assert col.inferred_type is InferredType.DATE
    assert col.type_mixture.get("date") == 1.0


def test_booleans_need_low_cardinality():
    col = make_column(distinct=2, probes={"bool": 100})
    assert col.inferred_type is InferredType.BOOLEAN


def test_zero_and_one_at_high_cardinality_are_integers_not_booleans():
    """'0' and '1' satisfy both probes; cardinality is the tie-breaker."""
    col = make_column(distinct=50, probes={"bool": 100, "int": 100, "float": 100})
    assert col.inferred_type is InferredType.INTEGER


def test_emails():
    col = make_column(patterns={"email": 100})
    assert col.inferred_type is InferredType.EMAIL


def test_uuids():
    col = make_column(patterns={"uuid": 100})
    assert col.inferred_type is InferredType.UUID


def test_free_text():
    col = make_column(probes={}, patterns={})
    assert col.inferred_type is InferredType.TEXT


def test_empty_column():
    col = make_column(rows=100, nulls=100, distinct=0)
    assert col.inferred_type is InferredType.EMPTY
    assert col.has(Observation.EMPTY_COLUMN)


def test_blank_strings_do_not_dilute_confidence():
    """40 blanks out of 100 must not make 60 clean integers look mixed."""
    col = make_column(rows=100, blanks=40, probes={"int": 60, "float": 60})
    assert col.valued_count == 60
    assert col.inferred_type is InferredType.INTEGER
    assert col.type_confidence == 1.0


# ── the flagship case ─────────────────────────────────────────────────────────


def test_dates_with_a_few_epoch_timestamps():
    col = make_column(
        probes={"date": 97, "timestamp": 97, "int": 3, "float": 3},
        patterns={"iso_date": 97, "epoch_seconds": 3},
    )
    assert col.inferred_type is InferredType.MIXED
    assert col.has(Observation.MIXED_TYPES)
    assert col.has(Observation.EPOCH_SUSPECTED)
    assert col.type_mixture["date"] == 0.97
    assert col.type_mixture["integer"] == 0.03


def test_a_single_epoch_row_in_a_thousand_is_still_reported():
    """Three rows in a thousand is the leading edge of a breaking change."""
    col = make_column(
        rows=1000,
        distinct=900,
        probes={"date": 999, "timestamp": 999, "int": 1, "float": 1},
        patterns={"iso_date": 999, "epoch_seconds": 1},
    )
    assert col.has(Observation.EPOCH_SUSPECTED)
    assert col.has(Observation.MIXED_TYPES)


def test_epoch_shaped_ids_without_temporal_context_are_not_flagged():
    """A whole column of 10-digit integers is probably identifiers."""
    col = make_column(
        probes={"int": 100, "float": 100},
        patterns={"epoch_seconds": 40},
    )
    assert not col.has(Observation.EPOCH_SUSPECTED)


def test_a_column_entirely_of_epochs_is_flagged():
    col = make_column(probes={"int": 100, "float": 100}, patterns={"epoch_seconds": 100})
    assert col.has(Observation.EPOCH_SUSPECTED)


def test_excel_serials_need_corroboration():
    only_serials = make_column(probes={"int": 100, "float": 100}, patterns={"excel_serial": 100})
    assert only_serials.has(Observation.EXCEL_SERIAL_SUSPECTED)

    incidental = make_column(probes={"int": 100, "float": 100}, patterns={"excel_serial": 20})
    assert not incidental.has(Observation.EXCEL_SERIAL_SUSPECTED)


# ── declared versus inferred ──────────────────────────────────────────────────


def test_numeric_stored_as_text():
    col = make_column(declared="VARCHAR", probes={"int": 100, "float": 100})
    assert col.has(Observation.NUMERIC_STORED_AS_TEXT)


def test_temporal_stored_as_text():
    col = make_column(
        declared="VARCHAR", probes={"date": 100, "timestamp": 100}, patterns={"iso_date": 100}
    )
    assert col.has(Observation.TEMPORAL_STORED_AS_TEXT)


def test_declared_type_conflict():
    """A BIGINT column full of dates is a genuine schema contradiction."""
    col = make_column(
        declared="BIGINT", probes={"date": 100, "timestamp": 100}, patterns={"iso_date": 100}
    )
    assert col.has(Observation.DECLARED_TYPE_CONFLICT)


def test_integer_in_a_double_column_is_not_a_conflict():
    col = make_column(declared="DOUBLE", probes={"int": 100, "float": 100})
    assert not col.has(Observation.DECLARED_TYPE_CONFLICT)


def test_declared_family_mapping():
    assert declared_family("VARCHAR") == "text"
    assert declared_family("BIGINT") == "integer"
    assert declared_family("DOUBLE") == "float"
    assert declared_family("NUMERIC(10,2)") == "float"
    assert declared_family("TIMESTAMP WITH TIME ZONE") == "timestamp"
    assert declared_family("DATE") == "date"
    assert declared_family("BOOLEAN") == "boolean"
    assert declared_family("unknown") == "unknown"


# ── hygiene observations ──────────────────────────────────────────────────────


def test_inconsistent_case_requires_letters_in_most_values():
    """A date column with 'N/A' and 'null' must not be reported for casing."""
    mostly_dates = make_column(
        probes={"date": 25, "timestamp": 25},
        patterns={"iso_date": 25, "null_word": 3},
        text=TextStats(uppercase_count=1, lowercase_count=2, mixed_case_count=0),
    )
    assert not mostly_dates.has(Observation.INCONSISTENT_CASE)

    real_text = make_column(
        text=TextStats(uppercase_count=40, lowercase_count=40, mixed_case_count=20),
    )
    assert real_text.has(Observation.INCONSISTENT_CASE)


def test_leading_zeros_are_reported_from_a_single_occurrence():
    col = make_column(probes={"int": 100, "float": 100}, patterns={"leading_zeros": 1})
    assert col.has(Observation.LEADING_ZEROS)


def test_high_null_rate():
    col = make_column(rows=100, nulls=60, distinct=40, probes={"int": 40, "float": 40})
    assert col.has(Observation.HIGH_NULL_RATE)


def test_unique_and_constant_are_mutually_exclusive():
    unique = make_column(rows=100, distinct=100)
    constant = make_column(rows=100, distinct=1)
    assert unique.has(Observation.UNIQUE) and not unique.has(Observation.CONSTANT)
    assert constant.has(Observation.CONSTANT) and not constant.has(Observation.UNIQUE)


# ── PII ───────────────────────────────────────────────────────────────────────


def test_pii_in_free_text_is_distinguished_from_an_email_column():
    notes = make_column(patterns={"email_embedded": 20})
    assert notes.has(Observation.PII_IN_FREE_TEXT)
    assert notes.has(Observation.PII_DETECTED)

    email_column = make_column(patterns={"email": 100, "email_embedded": 100})
    assert email_column.has(Observation.PII_DETECTED)
    assert not email_column.has(Observation.PII_IN_FREE_TEXT)


def test_postal_shaped_numbers_do_not_raise_pii():
    col = make_column(probes={"int": 100, "float": 100}, patterns={"postal_like": 100})
    assert not col.has(Observation.PII_DETECTED)
    assert col.pii_signals == []


def test_boolean_spelled_many_ways():
    """A flag column with true/TRUE/yes/1/t is boolean with a vocabulary problem."""
    col = make_column(
        distinct=8,
        probes={"bool": 97, "int": 21, "float": 21},
    )
    assert col.has(Observation.MIXED_BOOLEAN_ENCODING)
    assert col.inferred_type is not InferredType.BOOLEAN


def test_a_clean_boolean_is_not_flagged_for_encoding():
    col = make_column(distinct=2, probes={"bool": 100})
    assert col.inferred_type is InferredType.BOOLEAN
    assert not col.has(Observation.MIXED_BOOLEAN_ENCODING)
