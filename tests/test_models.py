"""Model behaviour: derived properties, serialisation hygiene, fingerprinting."""

from __future__ import annotations

import json

from zeyvor.profile.models import (
    PROFILE_SCHEMA_VERSION,
    ColumnProfile,
    EnumMember,
    EnumProfile,
    InferredType,
    NumericStats,
    Observation,
    ShapeBucket,
    TableProfile,
    TextStats,
)


def column(**kwargs) -> ColumnProfile:
    defaults = dict(name="c", row_count=100, null_count=0, blank_count=0, distinct_count=100)
    defaults.update(kwargs)
    return ColumnProfile(**defaults)


# ── derived properties ────────────────────────────────────────────────────────


def test_counts_are_consistent():
    col = column(row_count=100, null_count=10, blank_count=5, distinct_count=80)
    assert col.non_null_count == 90
    assert col.valued_count == 85
    assert col.null_rate == 0.1
    assert col.distinct_rate == round(80 / 90, 6)


def test_uniqueness_ignores_nulls():
    col = column(row_count=100, null_count=10, distinct_count=90)
    assert col.is_unique


def test_zero_rows_never_divides_by_zero():
    col = column(row_count=0, distinct_count=0)
    assert col.null_rate is None
    assert col.distinct_rate is None
    assert col.pattern_rate("email") == 0.0
    assert not col.is_unique
    assert not col.is_constant
    assert not col.is_empty


def test_pattern_rate_uses_valued_count():
    """Blanks must not dilute a pattern's share."""
    col = column(row_count=100, blank_count=50, pattern_hits={"email": 50})
    assert col.valued_count == 50
    assert col.pattern_rate("email") == 1.0


def test_shape_coverage_is_capped_at_one():
    col = column(row_count=10, shapes=[ShapeBucket("#", 10), ShapeBucket("a", 5)])
    assert col.shape_coverage == 1.0


def test_dominant_shape_is_the_first():
    col = column(shapes=[ShapeBucket("####-##-##", 97), ShapeBucket("##########", 3)])
    assert col.dominant_shape.shape == "####-##-##"


def test_pii_signals_only_lists_pii_patterns():
    col = column(pattern_hits={"email": 5, "postal_like": 99, "iso_date": 12})
    assert col.pii_signals == ["email"]


def test_has_accepts_enum_or_string():
    col = column(observations=[Observation.UNIQUE.value])
    assert col.has(Observation.UNIQUE)
    assert col.has("unique")
    assert not col.has(Observation.CONSTANT)


# ── serialisation ─────────────────────────────────────────────────────────────


def test_to_dict_omits_empty_values():
    """Profiles get committed to git, so noise costs review attention."""
    data = column().to_dict()
    assert "numeric" not in data
    assert "enum" not in data
    assert "samples" not in data
    assert "observations" not in data
    assert data["name"] == "c"


def test_zero_counts_are_omitted_but_real_zeros_survive():
    col = column(null_count=0)
    data = col.to_dict()
    assert "blank_count" not in data
    assert data["null_count"] == 0  # meaningful: explicitly measured as zero


def test_column_round_trip_preserves_everything():
    original = column(
        name="amount",
        declared_type="DOUBLE",
        inferred_type=InferredType.FLOAT,
        type_confidence=0.99,
        type_mixture={"float": 1.0},
        type_probes={"float": 100},
        pattern_hits={"currency": 3},
        numeric=NumericStats(minimum=1.0, maximum=9.0, mean=5.0, parseable_count=100),
        text=TextStats(min_length=1, max_length=4),
        shapes=[ShapeBucket("#.##", 100, 1.0)],
        enum=EnumProfile(members=[EnumMember("a", 60, 0.6)], complete=True, cardinality=1),
        observations=["unique"],
    )
    restored = ColumnProfile.from_dict(json.loads(json.dumps(original.to_dict())))

    assert restored.name == "amount"
    assert restored.inferred_type is InferredType.FLOAT
    assert restored.numeric.maximum == 9.0
    assert restored.text.max_length == 4
    assert restored.shapes[0].shape == "#.##"
    assert restored.enum.members[0].value == "a"
    assert restored.observations == ["unique"]
    assert restored.pattern_hits == {"currency": 3}


def test_table_round_trip_and_metadata():
    profile = TableProfile(
        name="orders",
        source_uri="orders.csv",
        row_count=10,
        columns=[column(name="a"), column(name="b")],
    )
    data = profile.to_dict()
    assert data["schema_version"] == PROFILE_SCHEMA_VERSION
    assert data["column_count"] == 2
    assert data["fingerprint"].startswith("sha256:")

    restored = TableProfile.from_json(profile.to_json())
    assert restored.column_count == 2
    assert restored.column("b").name == "b"


def test_json_is_human_readable():
    profile = TableProfile(name="t", columns=[column()])
    text = profile.to_json()
    assert "\n" in text and '"name": "c"' in text


# ── fingerprinting ────────────────────────────────────────────────────────────


def base_profile() -> TableProfile:
    return TableProfile(
        name="orders",
        row_count=100,
        columns=[
            column(
                name="signup_date",
                inferred_type=InferredType.DATE,
                shapes=[ShapeBucket("####-##-##", 100, 1.0)],
            ),
            column(
                name="status",
                distinct_count=2,
                inferred_type=InferredType.TEXT,
                enum=EnumProfile(
                    members=[EnumMember("shipped", 60), EnumMember("pending", 40)],
                    complete=True,
                    cardinality=2,
                ),
            ),
        ],
    )


def test_fingerprint_ignores_volume_and_timing():
    """Re-running on more data must not look like a change.

    Volume scales; structure does not. The unique column stays unique and the
    category set stays the same size, so the digest must be identical.
    """
    small = base_profile()
    large = base_profile()
    large.row_count = 5_000_000
    large.duration_ms = 9999
    large.profiled_at = "2030-01-01T00:00:00+00:00"
    for col in large.columns:
        col.row_count = 5_000_000
    # signup_date is a unique column, so its distinct count grows with the table.
    large.column("signup_date").distinct_count = 5_000_000
    assert small.fingerprint() == large.fingerprint()


def test_fingerprint_changes_when_a_key_stops_being_unique():
    """Losing uniqueness is structural, and worth waking someone up for."""
    before = base_profile()
    after = base_profile()
    after.column("signup_date").row_count = 200  # duplicates appeared
    assert before.fingerprint() != after.fingerprint()


def test_fingerprint_ignores_column_order():
    forward = base_profile()
    reversed_ = base_profile()
    reversed_.columns.reverse()
    assert forward.fingerprint() == reversed_.fingerprint()


def test_fingerprint_changes_on_a_new_category():
    before = base_profile()
    after = base_profile()
    after.column("status").enum.members.append(EnumMember("awaiting_pickup", 5))
    assert before.fingerprint() != after.fingerprint()


def test_fingerprint_changes_on_a_type_change():
    before = base_profile()
    after = base_profile()
    after.column("signup_date").inferred_type = InferredType.INTEGER
    assert before.fingerprint() != after.fingerprint()


def test_fingerprint_changes_on_a_format_change():
    before = base_profile()
    after = base_profile()
    after.column("signup_date").shapes = [ShapeBucket("##########", 100, 1.0)]
    assert before.fingerprint() != after.fingerprint()


def test_fingerprint_changes_on_a_rename():
    before = base_profile()
    after = base_profile()
    after.columns[0].name = "created_date"
    assert before.fingerprint() != after.fingerprint()


def test_incomplete_category_sets_are_excluded_from_the_fingerprint():
    """A truncated set is not ground truth, so it must not create false churn."""
    before = base_profile()
    after = base_profile()
    after.column("status").enum.complete = False
    other = base_profile()
    other.column("status").enum.complete = False
    other.column("status").enum.members.append(EnumMember("new_state", 1))
    assert after.fingerprint() == other.fingerprint()
    assert before.fingerprint() != after.fingerprint()


def test_enum_values_helper():
    enum = EnumProfile(members=[EnumMember("a", 2), EnumMember("b", 1)], cardinality=2)
    assert enum.values() == ["a", "b"]
