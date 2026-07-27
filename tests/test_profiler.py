"""End-to-end profiling against real files, and the invariants that matter."""

from __future__ import annotations

import pytest

from helpers import fixture_path
from zeyvor import DuckDBEngine
from zeyvor.engines.base import Relation
from zeyvor.profile import InferredType, Observation, PrivacyMode, ProfileOptions, Profiler
from zeyvor.profile.models import TableProfile
from zeyvor.sources import resolve_source


class RecordingEngine(DuckDBEngine):
    """Wraps DuckDB to record every statement and how many rows came back."""

    def __init__(self) -> None:
        super().__init__()
        self.statements: list[str] = []
        self.rows_returned = 0

    def execute(self, sql: str):
        self.statements.append(sql)
        rows = super().execute(sql)
        self.rows_returned += len(rows)
        return rows


def profile_with(engine, name: str, options: ProfileOptions | None = None) -> TableProfile:
    resolved = resolve_source(fixture_path(name), engine=engine)
    return Profiler(engine, options).profile(resolved.relation)


# ── basic mechanics ───────────────────────────────────────────────────────────


def test_counts_and_shape(profile_fixture):
    profile = profile_fixture("clean_orders.csv")
    assert profile.row_count == 100
    assert profile.column_count == 7
    assert [c.position for c in profile.columns] == list(range(7))
    assert profile.name == "clean_orders"
    assert profile.dialect == "duckdb"
    assert profile.duration_ms >= 0


def test_declared_types_come_from_the_source(profile_fixture):
    profile = profile_fixture("clean_orders.csv")
    assert profile.column("order_id").declared_type == "BIGINT"
    assert profile.column("amount").declared_type == "DOUBLE"
    assert profile.column("signup_date").declared_type == "DATE"
    assert profile.column("status").declared_type == "VARCHAR"


def test_inferred_types_are_independent_of_declared_types(profile_fixture):
    profile = profile_fixture("clean_orders.csv")
    assert profile.column("order_id").inferred_type is InferredType.INTEGER
    assert profile.column("amount").inferred_type is InferredType.FLOAT
    assert profile.column("signup_date").inferred_type is InferredType.DATE
    assert profile.column("customer_email").inferred_type is InferredType.EMAIL


def test_a_clean_column_produces_no_findings(profile_fixture):
    """False positives are the failure mode that gets a checker uninstalled."""
    profile = profile_fixture("clean_orders.csv")
    assert profile.column("signup_date").observations == []


def test_null_and_blank_accounting(profile_fixture):
    profile = profile_fixture("edge_cases.csv")
    always_null = profile.column("always_null")
    assert always_null.null_count == 3
    assert always_null.non_null_count == 0
    assert always_null.is_empty

    always_blank = profile.column("always_blank")
    # Empty CSV fields read as NULL; either accounting must yield zero values.
    assert always_blank.valued_count == 0
    assert always_blank.inferred_type is InferredType.EMPTY


def test_uniqueness_and_constancy(profile_fixture):
    profile = profile_fixture("edge_cases.csv")
    assert profile.column("id").is_unique
    assert profile.column("constant").is_constant


def test_numeric_stats_are_populated(profile_fixture):
    profile = profile_fixture("clean_orders.csv")
    stats = profile.column("amount").numeric
    assert stats is not None
    assert stats.minimum == pytest.approx(19.99)
    assert stats.maximum == pytest.approx(366.49)
    assert stats.p25 < stats.p50 < stats.p75
    assert stats.parseable_count == 100


def test_temporal_stats_are_populated(profile_fixture):
    profile = profile_fixture("clean_orders.csv")
    stats = profile.column("signup_date").temporal
    assert stats is not None
    assert stats.minimum.startswith("2024-01")
    assert stats.parseable_count == 100


def test_integer_columns_do_not_get_nonsense_date_ranges(profile_fixture):
    """Guard against an engine happily casting a digit string to a timestamp."""
    profile = profile_fixture("clean_orders.csv")
    assert profile.column("order_id").temporal is None


def test_text_stats_and_shapes(profile_fixture):
    profile = profile_fixture("clean_orders.csv")
    email = profile.column("customer_email")
    assert email.text is not None
    assert email.text.min_length and email.text.max_length
    assert email.dominant_shape is not None
    assert email.shape_coverage == pytest.approx(1.0)
    assert profile.column("signup_date").dominant_shape.shape == "####-##-##"


def test_shapes_are_sorted_by_frequency(profile_fixture):
    profile = profile_fixture("broken_dates.csv")
    shapes = profile.column("signup_date").shapes
    assert [s.shape for s in shapes[:2]] == ["####-##-##", "##########"]
    assert shapes[0].count > shapes[1].count


# ── categories ────────────────────────────────────────────────────────────────


def test_low_cardinality_columns_record_their_full_value_set(profile_fixture):
    profile = profile_fixture("clean_orders.csv")
    status = profile.column("status")
    assert status.enum is not None
    assert status.enum.complete
    assert set(status.enum.values()) == {"pending", "shipped", "delivered", "refunded"}
    assert status.has(Observation.ENUM_CANDIDATE)


def test_unique_columns_are_never_treated_as_categories(profile_fixture):
    """Otherwise the 'enum' is a dump of every customer name."""
    profile = profile_fixture("messy.csv")
    for name in ("full_name", "revenue", "account_code"):
        column = profile.column(name)
        assert column.is_unique
        assert column.enum is None
        assert not column.has(Observation.ENUM_CANDIDATE)


def test_high_cardinality_columns_are_not_categories(profile_fixture):
    profile = profile_fixture("clean_orders.csv", ProfileOptions(max_enum_cardinality=3))
    assert profile.column("status").enum is None


def test_enum_completeness_reflects_truncation(profile_fixture):
    profile = profile_fixture(
        "enum_drift.csv", ProfileOptions(enum_member_limit=2, max_enum_cardinality=2)
    )
    status = profile.column("status")
    # Cardinality (5) exceeds the cap, so no category set should be claimed.
    assert status.enum is None


# ── pushdown, the core architectural promise ──────────────────────────────────


def test_rows_are_never_fetched():
    """The whole design rests on measuring in place.

    A 100-row table must not produce 100 rows of traffic. Only aggregates,
    shape histograms and category sets come back.
    """
    engine = RecordingEngine()
    try:
        profile = profile_with(engine, "clean_orders.csv")
        assert profile.row_count == 100
        assert engine.rows_returned < 60, (
            f"fetched {engine.rows_returned} rows; profiling should return only aggregates"
        )
    finally:
        engine.close()


def test_no_samples_are_collected_by_default():
    engine = RecordingEngine()
    try:
        profile = profile_with(engine, "clean_orders.csv")
        assert all(column.samples == [] for column in profile.columns)
        assert not any("LIMIT 5" in statement for statement in engine.statements)
    finally:
        engine.close()


def test_query_count_does_not_scale_with_column_count(profile_fixture):
    narrow = profile_fixture("clean_orders.csv")  # 7 columns
    wide = profile_fixture("wide.csv")  # 60 columns
    assert narrow.query_count <= 8
    assert wide.query_count <= 16, "wide tables must not cost a query per column"


def test_batch_size_trades_round_trips_for_query_width(profile_fixture):
    small_batches = profile_fixture("wide.csv", ProfileOptions(column_batch_size=5))
    large_batches = profile_fixture("wide.csv", ProfileOptions(column_batch_size=60))
    assert small_batches.query_count > large_batches.query_count
    # The result must be identical regardless of how the work was divided.
    assert small_batches.fingerprint() == large_batches.fingerprint()


def test_wide_tables_are_profiled_completely(profile_fixture):
    profile = profile_fixture("wide.csv", ProfileOptions(column_batch_size=7))
    assert profile.column_count == 60
    assert all(column.row_count == 20 for column in profile.columns)
    assert all(column.distinct_count == 20 for column in profile.columns)


# ── privacy ───────────────────────────────────────────────────────────────────


def test_masked_mode_keeps_category_vocabulary(profile_fixture):
    profile = profile_fixture("clean_orders.csv", ProfileOptions(privacy=PrivacyMode.MASKED))
    assert "shipped" in profile.column("status").enum.values()
    assert profile.privacy_mode == "masked"


def test_strict_mode_hashes_categories(profile_fixture):
    profile = profile_fixture("clean_orders.csv", ProfileOptions(privacy=PrivacyMode.STRICT))
    status = profile.column("status")
    assert status.enum.hashed
    assert all(value.startswith("sha256:") for value in status.enum.values())
    assert "shipped" not in status.enum.values()


def test_strict_mode_still_detects_everything(profile_fixture):
    """Privacy must not cost accuracy — the findings are identical."""
    strict = profile_fixture("broken_dates.csv", ProfileOptions(privacy=PrivacyMode.STRICT))
    masked = profile_fixture("broken_dates.csv", ProfileOptions(privacy=PrivacyMode.MASKED))
    assert strict.column("signup_date").observations == masked.column("signup_date").observations
    assert strict.column("signup_date").inferred_type is masked.column("signup_date").inferred_type


def test_full_mode_collects_samples(profile_fixture):
    profile = profile_fixture("clean_orders.csv", ProfileOptions(privacy=PrivacyMode.FULL))
    samples = profile.column("status").samples
    assert samples and len(samples) <= 5


def test_text_extremes_are_never_collected_in_any_mode(profile_fixture):
    """Alphabetical min/max of a text column would be real customer data.

    The profiler never asks for them; only lengths are recorded.
    """
    for mode in (PrivacyMode.STRICT, PrivacyMode.MASKED, PrivacyMode.FULL):
        profile = profile_fixture("clean_orders.csv", ProfileOptions(privacy=mode))
        email = profile.column("customer_email")
        assert email.text is not None
        assert not hasattr(email.text, "minimum")
        assert isinstance(email.text.min_length, int)


# ── serialisation and fingerprinting ──────────────────────────────────────────


def test_json_round_trip(profile_fixture):
    profile = profile_fixture("messy.csv")
    restored = TableProfile.from_json(profile.to_json())
    assert restored.row_count == profile.row_count
    assert restored.column_count == profile.column_count
    assert restored.fingerprint() == profile.fingerprint()
    for original, copy in zip(profile.columns, restored.columns, strict=True):
        assert copy.name == original.name
        assert copy.inferred_type is original.inferred_type
        assert copy.observations == original.observations
        assert copy.pattern_hits == original.pattern_hits


def test_fingerprint_is_stable_across_runs(profile_fixture):
    first = profile_fixture("clean_orders.csv")
    second = profile_fixture("clean_orders.csv")
    assert first.fingerprint() == second.fingerprint()


def test_fingerprint_changes_when_meaning_changes(profile_fixture):
    clean = profile_fixture("clean_orders.csv")
    broken = profile_fixture("broken_dates.csv")
    assert clean.fingerprint() != broken.fingerprint()


def test_profile_saves_and_loads(tmp_path, profile_fixture):
    profile = profile_fixture("clean_orders.csv")
    path = tmp_path / "profile.json"
    profile.save(str(path))
    assert TableProfile.load(str(path)).fingerprint() == profile.fingerprint()


def test_column_lookup_helpers(profile_fixture):
    profile = profile_fixture("clean_orders.csv")
    assert profile.column("status").name == "status"
    assert profile.get("nope") is None
    with pytest.raises(KeyError):
        profile.column("nope")
    assert profile.columns_with(Observation.ENUM_CANDIDATE)


# ── robustness ────────────────────────────────────────────────────────────────


def test_empty_relation_does_not_crash(duck):
    """Zero rows is a legitimate state, not an error."""
    relation = Relation(
        sql="(SELECT 1 AS a, 'x' AS b WHERE 1 = 0)", name="empty", source_uri="test"
    )
    profile = Profiler(duck).profile(relation)
    assert profile.row_count == 0
    assert profile.column_count == 2
    assert all(column.inferred_type is InferredType.EMPTY for column in profile.columns)


def test_single_row_relation(duck):
    relation = Relation(sql="(SELECT 42 AS a)", name="one", source_uri="test")
    profile = Profiler(duck).profile(relation)
    assert profile.row_count == 1
    assert profile.column("a").inferred_type is InferredType.INTEGER


def test_awkward_column_names_are_quoted(duck):
    relation = Relation(
        sql="""(SELECT 1 AS "order id", 2 AS "we ""quoted"" it", 3 AS "Ünïcode")""",
        name="awkward",
        source_uri="test",
    )
    profile = Profiler(duck).profile(relation)
    assert {c.name for c in profile.columns} == {"order id", 'we "quoted" it', "Ünïcode"}


def test_unreadable_source_raises_clearly():
    with pytest.raises(FileNotFoundError):
        resolve_source("/definitely/not/here.csv")
