"""Each violation in isolation, plus the compatibility and suppression rules.

Profiles are built by hand here rather than measured, which keeps every test to
a single variable and makes the boundaries between violations explicit.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from zeyvor.contract import ColumnContract, Contract, Severity, TableContract, ViolationType, check
from zeyvor.contract.diff import family_accepts, type_accepts
from zeyvor.profile.models import (
    ColumnProfile,
    EnumMember,
    EnumProfile,
    InferredType,
    NumericStats,
    ShapeBucket,
    TableProfile,
    TemporalStats,
)


def column_profile(**kwargs) -> ColumnProfile:
    defaults = {
        "name": "c",
        "row_count": 100,
        "null_count": 0,
        "blank_count": 0,
        "distinct_count": 100,
    }
    defaults.update(kwargs)
    return ColumnProfile(**defaults)


def profile_of(*columns: ColumnProfile, name: str = "t", rows: int = 100) -> TableProfile:
    return TableProfile(name=name, row_count=rows, columns=list(columns))


def contract_of(column: ColumnContract, *, name: str = "t", **table_kwargs) -> Contract:
    return Contract(
        tables={name: TableContract(name=name, columns={column.name: column}, **table_kwargs)}
    )


def run(profile: TableProfile, column: ColumnContract, **table_kwargs):
    return check(profile, contract_of(column, name=profile.name, **table_kwargs))


# ── type compatibility ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "contracted,found,accepted",
    [
        ("integer", "integer", True),
        ("float", "integer", True),  # integers are valid floats
        ("integer", "float", False),  # floats in an int column lose precision
        ("timestamp", "date", True),
        ("date", "timestamp", False),
        ("text", "email", True),  # a text contract promises nothing more
        ("email", "text", False),  # emails becoming junk is a narrowing
        ("date", "integer", False),
    ],
)
def test_type_compatibility(contracted, found, accepted):
    assert type_accepts(contracted, found) is accepted


def test_mixture_families_are_coarser_than_types():
    """The profile's mixture only knows integer/float/date/timestamp/text.

    Emails, UUIDs and booleans all land in the text residual, so contamination
    has to be judged at family level — comparing 'email' against 'text' directly
    once reported a perfectly good email column as 100% contaminated.
    """
    assert family_accepts("email", "text")
    assert family_accepts("boolean", "text")
    assert family_accepts("uuid", "text")
    # Text sits at the top of the lattice: anything satisfies it.
    assert family_accepts("text", "integer")
    # But a date contract accepts only dates.
    assert not family_accepts("date", "integer")


# ── type violations ───────────────────────────────────────────────────────────


def test_type_changed():
    profile = profile_of(column_profile(inferred_type=InferredType.TEXT, type_confidence=1.0))
    report = run(profile, ColumnContract(name="c", type="date"))

    assert report.has(ViolationType.TYPE_CHANGED)
    violation = report.of_type(ViolationType.TYPE_CHANGED)[0]
    assert "calendar dates" in violation.expected
    assert "text" in violation.found
    assert "type: text" in violation.remedy


def test_a_compatible_type_is_not_a_violation():
    profile = profile_of(
        column_profile(inferred_type=InferredType.INTEGER, type_mixture={"integer": 1.0})
    )
    assert check(profile, contract_of(ColumnContract(name="c", type="float"))).ok


def test_type_contaminated_when_the_contracted_type_still_dominates():
    profile = profile_of(
        column_profile(
            inferred_type=InferredType.DATE,
            type_mixture={"date": 0.998, "integer": 0.002},
            shapes=[ShapeBucket("####-##-##", 998), ShapeBucket("##########", 2)],
        )
    )
    report = run(profile, ColumnContract(name="c", type="date"))

    assert report.has(ViolationType.TYPE_CONTAMINATED)
    assert not report.has(ViolationType.TYPE_CHANGED)
    assert report.of_type(ViolationType.TYPE_CONTAMINATED)[0].evidence["offending"] == {
        "integer": 0.002
    }


def test_a_mixed_column_is_contamination_when_the_contract_still_holds_the_majority():
    profile = profile_of(
        column_profile(inferred_type=InferredType.MIXED, type_mixture={"date": 0.7, "integer": 0.3})
    )
    report = run(profile, ColumnContract(name="c", type="date"))
    assert report.has(ViolationType.TYPE_CONTAMINATED)


def test_a_mixed_column_is_a_type_change_when_the_contract_lost_the_majority():
    profile = profile_of(
        column_profile(inferred_type=InferredType.MIXED, type_mixture={"date": 0.2, "integer": 0.8})
    )
    report = run(profile, ColumnContract(name="c", type="date"))
    assert report.has(ViolationType.TYPE_CHANGED)
    assert not report.has(ViolationType.TYPE_CONTAMINATED)


def test_contamination_below_the_floor_is_silent():
    profile = profile_of(
        column_profile(
            row_count=1_000_000,
            inferred_type=InferredType.DATE,
            type_mixture={"date": 0.9999, "integer": 0.0001},
        )
    )
    assert check(profile, contract_of(ColumnContract(name="c", type="date"))).ok


def test_an_emptied_column_is_a_presence_problem_not_a_type_problem():
    profile = profile_of(
        column_profile(
            row_count=100, null_count=100, distinct_count=0, inferred_type=InferredType.EMPTY
        )
    )
    report = run(profile, ColumnContract(name="c", type="date", nullable=False))

    assert report.has(ViolationType.NULLABILITY_VIOLATED)
    assert not report.has(ViolationType.TYPE_CHANGED)


# ── format ────────────────────────────────────────────────────────────────────


def test_format_changed():
    profile = profile_of(
        column_profile(
            inferred_type=InferredType.TEXT,
            type_mixture={"text": 1.0},
            shapes=[ShapeBucket("aa", 90, 0.9), ShapeBucket("aaa", 10, 0.1)],
        )
    )
    report = run(profile, ColumnContract(name="c", type="text", formats=["aa"]))

    assert report.has(ViolationType.FORMAT_CHANGED)
    assert "'aaa'" in report.of_type(ViolationType.FORMAT_CHANGED)[0].found


def test_a_negligible_new_shape_is_tolerated():
    profile = profile_of(
        column_profile(
            inferred_type=InferredType.TEXT,
            type_mixture={"text": 1.0},
            shapes=[ShapeBucket("aa", 999, 0.999), ShapeBucket("aaa", 1, 0.001)],
        )
    )
    assert check(profile, contract_of(ColumnContract(name="c", type="text", formats=["aa"]))).ok


# ── categories ────────────────────────────────────────────────────────────────


def test_new_category():
    profile = profile_of(
        column_profile(
            inferred_type=InferredType.TEXT,
            type_mixture={"text": 1.0},
            distinct_count=3,
            enum=EnumProfile(
                members=[EnumMember("a", 50), EnumMember("b", 40), EnumMember("c", 10)],
                complete=True,
                cardinality=3,
            ),
        )
    )
    report = run(
        profile,
        ColumnContract(name="c", type="text", categories=["a", "b"], categories_closed=True),
    )

    assert report.has(ViolationType.NEW_CATEGORY)
    assert report.of_type(ViolationType.NEW_CATEGORY)[0].evidence["new_categories"] == ["c"]


def test_an_open_category_set_never_complains():
    profile = profile_of(
        column_profile(
            inferred_type=InferredType.TEXT,
            type_mixture={"text": 1.0},
            distinct_count=2,
            enum=EnumProfile(members=[EnumMember("z", 100)], complete=True, cardinality=1),
        )
    )
    contract = ColumnContract(name="c", type="text", categories=["a"], categories_closed=False)
    assert check(profile, contract_of(contract)).ok


def test_categories_that_could_not_be_verified_warn_rather_than_pass_silently():
    """Silently stopping a check the contract claims to perform is the worst
    of the three options."""
    profile = profile_of(
        column_profile(
            inferred_type=InferredType.TEXT,
            type_mixture={"text": 1.0},
            distinct_count=5000,
            enum=None,
        )
    )
    report = run(
        profile,
        ColumnContract(name="c", type="text", categories=["a"], categories_closed=True),
    )

    assert report.has(ViolationType.CATEGORIES_UNVERIFIABLE)
    assert report.ok  # a warning, not a failure
    assert "max_enum_cardinality" in report.violations[0].remedy


# ── presence and uniqueness ───────────────────────────────────────────────────


def test_nullability_violated_suggests_a_tolerance():
    profile = profile_of(column_profile(row_count=100, null_count=3, distinct_count=97))
    report = run(profile, ColumnContract(name="c", nullable=False))

    violation = report.of_type(ViolationType.NULLABILITY_VIOLATED)[0]
    assert "3 of 100 rows" in violation.found
    assert "max_null_rate" in violation.remedy


def test_null_rate_exceeded():
    profile = profile_of(column_profile(row_count=100, null_count=10, distinct_count=90))
    report = run(profile, ColumnContract(name="c", nullable=True, max_null_rate=0.05))

    assert report.has(ViolationType.NULL_RATE_EXCEEDED)


def test_a_null_rate_within_tolerance_passes():
    profile = profile_of(column_profile(row_count=100, null_count=2, distinct_count=98))
    contract = ColumnContract(name="c", nullable=True, max_null_rate=0.05)
    assert check(profile, contract_of(contract)).ok


def test_uniqueness_lost():
    profile = profile_of(column_profile(row_count=100, distinct_count=90))
    report = run(profile, ColumnContract(name="c", unique=True))

    violation = report.of_type(ViolationType.UNIQUENESS_LOST)[0]
    assert "10 duplicate values" in violation.found
    assert "fan out" in violation.detail


def test_an_approximate_distinct_count_cannot_prove_duplication():
    profile = profile_of(column_profile(row_count=100, distinct_count=90, distinct_is_approx=True))
    assert check(profile, contract_of(ColumnContract(name="c", unique=True))).ok


# ── ranges ────────────────────────────────────────────────────────────────────


def test_range_exceeded_numeric():
    profile = profile_of(
        column_profile(
            inferred_type=InferredType.FLOAT,
            type_mixture={"float": 1.0},
            numeric=NumericStats(minimum=-5.0, maximum=150.0, parseable_count=100),
        )
    )
    report = run(profile, ColumnContract(name="c", type="float", minimum=0, maximum=200))

    violation = report.of_type(ViolationType.RANGE_EXCEEDED)[0]
    assert "minimum -5.0 is below 0" in violation.found


def test_a_tenfold_breach_is_reported_as_a_unit_change():
    profile = profile_of(
        column_profile(
            inferred_type=InferredType.FLOAT,
            type_mixture={"float": 1.0},
            numeric=NumericStats(minimum=20.0, maximum=9000.0, parseable_count=100),
        )
    )
    report = run(profile, ColumnContract(name="c", type="float", minimum=0, maximum=200))

    assert report.has(ViolationType.UNIT_SHIFT_SUSPECTED)
    assert not report.has(ViolationType.RANGE_EXCEEDED)


def test_temporal_ranges_and_the_today_token():
    profile = profile_of(
        column_profile(
            inferred_type=InferredType.DATE,
            type_mixture={"date": 1.0},
            temporal=TemporalStats(minimum="2024-01-01", maximum="2099-12-31", parseable_count=100),
        )
    )
    report = run(
        profile, ColumnContract(name="c", type="date", minimum="2019-01-01", maximum="today")
    )

    assert report.has(ViolationType.RANGE_EXCEEDED)
    assert "2099-12-31" in report.violations[0].found


def test_a_date_within_a_moving_bound_passes():
    profile = profile_of(
        column_profile(
            inferred_type=InferredType.DATE,
            type_mixture={"date": 1.0},
            temporal=TemporalStats(minimum="2024-01-01", maximum="2024-06-01", parseable_count=100),
        )
    )
    contract = ColumnContract(name="c", type="date", minimum="2019-01-01", maximum="today")
    assert check(profile, contract_of(contract)).ok


# ── privacy ───────────────────────────────────────────────────────────────────


def test_pii_appeared():
    profile = profile_of(
        column_profile(
            inferred_type=InferredType.TEXT,
            type_mixture={"text": 1.0},
            pattern_hits={"email_embedded": 47},
        )
    )
    report = run(profile, ColumnContract(name="c", type="text", no_pii=True))

    violation = report.of_type(ViolationType.PII_APPEARED)[0]
    assert "email_embedded (47 rows)" in violation.found
    assert "downstream" in violation.detail


# ── observations, and the accepted-state escape hatch ─────────────────────────


def test_an_observation_becomes_a_violation():
    profile = profile_of(
        column_profile(
            inferred_type=InferredType.TEXT,
            type_mixture={"text": 1.0},
            observations=["mojibake"],
        )
    )
    report = run(profile, ColumnContract(name="c", type="text"))

    assert report.has(ViolationType.MOJIBAKE_APPEARED)
    assert report.ok  # hygiene warns rather than blocks


def test_a_known_issue_is_not_reported_again():
    """Without this, generating a contract from imperfect data would warn on the
    very next run."""
    profile = profile_of(
        column_profile(
            inferred_type=InferredType.TEXT,
            type_mixture={"text": 1.0},
            observations=["mojibake"],
        )
    )
    contract = ColumnContract(name="c", type="text", known_issues=["mojibake"])
    assert check(profile, contract_of(contract)).violations == []


# ── table level ───────────────────────────────────────────────────────────────


def test_row_count_below_min():
    profile = profile_of(column_profile(row_count=3), rows=3)
    report = check(
        profile,
        Contract(
            tables={
                "t": TableContract(name="t", min_rows=100, columns={"c": ColumnContract(name="c")})
            }
        ),
    )
    assert report.has(ViolationType.ROW_COUNT_BELOW_MIN)
    assert "upstream job" in report.violations[0].detail


# ── suppression ───────────────────────────────────────────────────────────────


def test_a_changed_type_suppresses_its_own_consequences():
    """Format, range and category clauses are meaningless once the type is
    wrong; reporting them buries the finding that matters."""
    profile = profile_of(
        column_profile(
            inferred_type=InferredType.INTEGER,
            type_mixture={"integer": 1.0},
            shapes=[ShapeBucket("##########", 100, 1.0)],
            distinct_count=3,
            enum=EnumProfile(members=[EnumMember("1714089600", 100)], complete=True, cardinality=1),
            numeric=NumericStats(minimum=1.7e9, maximum=1.8e9, parseable_count=100),
        )
    )
    report = run(
        profile,
        ColumnContract(
            name="c",
            type="date",
            formats=["####-##-##"],
            categories=["2024-01-01"],
            categories_closed=True,
            minimum="2019-01-01",
            maximum="today",
        ),
    )

    assert report.has(ViolationType.TYPE_CHANGED)
    for suppressed in (
        ViolationType.FORMAT_CHANGED,
        ViolationType.NEW_CATEGORY,
        ViolationType.RANGE_EXCEEDED,
        ViolationType.TYPE_CONTAMINATED,
    ):
        assert not report.has(suppressed), f"{suppressed.value} should be suppressed"


# ── severity ──────────────────────────────────────────────────────────────────


def test_severity_precedence_is_column_then_table_then_default():
    profile = profile_of(
        column_profile(inferred_type=InferredType.TEXT, type_mixture={"text": 1.0})
    )
    column = ColumnContract(name="c", type="date")

    table = TableContract(name="t", columns={"c": column}, on_violation=Severity.WARN)
    contract = Contract(tables={"t": table})
    assert check(profile, contract).exit_code == 0

    column.on_violation = Severity.FAIL
    assert check(profile, contract).exit_code == 1


def test_raising_the_default_does_not_promote_hygiene_warnings():
    """A warn-level finding stays a warning unless something says otherwise,
    so tightening the contract does not turn every note into a build break."""
    profile = profile_of(
        column_profile(
            inferred_type=InferredType.TEXT,
            type_mixture={"text": 1.0},
            observations=["mojibake"],
        )
    )
    contract = contract_of(ColumnContract(name="c", type="text"))
    contract.defaults.on_violation = Severity.FAIL

    report = check(profile, contract)
    assert report.has(ViolationType.MOJIBAKE_APPEARED)
    assert report.exit_code == 0


def test_ignore_suppresses_a_column_entirely():
    profile = profile_of(
        column_profile(inferred_type=InferredType.TEXT, type_mixture={"text": 1.0})
    )
    contract = contract_of(ColumnContract(name="c", type="date", ignore=True))

    report = check(profile, contract)
    assert report.violations == []
    assert report.columns_checked == 0


# ── freshness ─────────────────────────────────────────────────────────────────
#
# The mirror of `max`, and the reason both exist: `max` catches a value from the
# future, this catches the absence of values from the present. A table whose
# loader stopped on Tuesday violates nothing else in a contract — every row is
# well formed, correctly typed, complete, and inside every stated range.


def _temporal_profile(newest: str, name: str = "loaded_at"):
    from zeyvor.profile.models import ColumnProfile, InferredType, TableProfile, TemporalStats

    column = ColumnProfile(name=name, row_count=100, distinct_count=100)
    column.inferred_type = InferredType.TIMESTAMP
    column.temporal = TemporalStats(minimum="2026-01-01", maximum=newest)
    return TableProfile(name="feed", row_count=100, columns=[column])


def _fresh_contract(window: str, name: str = "loaded_at"):
    return Contract(
        tables={
            "feed": TableContract(
                name="feed",
                columns={name: ColumnContract(name=name, type="timestamp", fresh_within=window)},
            )
        }
    )


def test_data_that_stopped_arriving_is_a_violation():
    now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    report = check([_temporal_profile("2026-08-05 09:00:00")], _fresh_contract("24h"), now=now)

    stale = [v for v in report.violations if v.type is ViolationType.STALE_DATA]
    assert len(stale) == 1
    assert "5 days old" in stale[0].found
    assert not report.ok


def test_recent_data_says_nothing():
    now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    report = check([_temporal_profile("2026-08-10 09:00:00")], _fresh_contract("24h"), now=now)
    assert report.ok


def test_the_window_is_the_boundary():
    """Exactly at the edge passes — a job that runs on the hour should not fail
    because the check ran a second after it."""
    now = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)
    on_time = check([_temporal_profile("2026-08-09 12:00:00")], _fresh_contract("24h"), now=now)
    assert on_time.ok

    late = check([_temporal_profile("2026-08-09 11:59:00")], _fresh_contract("24h"), now=now)
    assert not late.ok


def test_a_timezone_aware_timestamp_is_converted_not_ignored():
    """A warehouse handing back +02:00 must not be read as two hours staler."""
    now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    report = check([_temporal_profile("2026-08-10T12:30:00+02:00")], _fresh_contract("3h"), now=now)
    assert report.ok, "10:30 UTC is 90 minutes old, well inside a 3h window"


def test_an_unreadable_timestamp_is_skipped_rather_than_guessed():
    """Silence beats a wrong answer here: inventing an age would either fail a
    healthy table or, worse, pass a stopped one."""
    now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    report = check([_temporal_profile("whenever")], _fresh_contract("1h"), now=now)
    assert report.ok


def test_a_column_with_no_window_is_never_stale():
    now = datetime(2036, 1, 1, tzinfo=timezone.utc)
    contract = Contract(
        tables={
            "feed": TableContract(
                name="feed",
                columns={"loaded_at": ColumnContract(name="loaded_at", type="timestamp")},
            )
        }
    )
    report = check([_temporal_profile("2026-08-05 09:00:00")], contract, now=now)
    assert report.ok, "freshness must be opt-in, never assumed"
