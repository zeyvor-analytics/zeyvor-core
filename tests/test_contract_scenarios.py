"""End-to-end contract scenarios — the specification for Part 2.

Each test is a baseline captured on a good day, then the same table after
something changed. Between them they define what a contract is worth: it must be
silent when nothing is wrong, and specific when something is.
"""

from __future__ import annotations

import pytest

from zeyvor.contract import ViolationType, check, generate_contract

FIXTURES = [
    "clean_orders.csv",
    "messy.csv",
    "us_dates.csv",
    "edge_cases.csv",
    "enum_drift.csv",
    "excel_serials.csv",
    "unit_shift.csv",
    "unpadded_codes.csv",
    "wide.csv",
]


# ── the invariant everything else depends on ──────────────────────────────────


@pytest.mark.parametrize("fixture", FIXTURES)
def test_a_generated_contract_passes_against_its_own_source(fixture, profile_fixture):
    """The most important test in Part 2.

    If `zeyvor init` followed immediately by `zeyvor check` reports a problem,
    the tool has failed the user on their first run and will not get a second.
    This must hold even for `messy.csv`, whose data is full of genuine defects —
    those are recorded as accepted state rather than raised as news.
    """
    profile = profile_fixture(fixture)
    contract = generate_contract(profile)
    report = check(profile, contract)

    assert report.violations == [], (
        f"{fixture} produced false alarms on its own data:\n{report.render()}"
    )
    assert report.exit_code == 0
    assert report.ok


def test_a_contract_generated_from_messy_data_records_what_it_accepted(profile_fixture):
    """Silence on known mess must be deliberate and visible, not accidental."""
    profile = profile_fixture("messy.csv")
    contract = generate_contract(profile)
    notes = contract.tables["messy"].columns["notes"]

    assert "mojibake" in notes.known_issues
    # A reviewer reading the contract can see the defect was accepted, and
    # deleting the line turns the check back on.


# ── 1. the flagship: epoch timestamps in a date column ────────────────────────


def test_epoch_timestamps_break_a_date_contract(profile_fixture, baseline_contract):
    contract = baseline_contract("clean_orders.csv")
    broken = profile_fixture("broken_dates.csv", as_name="clean_orders")

    report = check(broken, contract)

    assert not report.ok
    assert report.exit_code == 1
    assert report.has(ViolationType.TYPE_CONTAMINATED, "signup_date")
    assert report.has(ViolationType.EPOCH_SUSPECTED, "signup_date")

    contamination = report.of_type(ViolationType.TYPE_CONTAMINATED)[0]
    assert "calendar dates" in contamination.expected
    assert "97.0% date" in contamination.found
    assert "3.0% integer" in contamination.found
    assert "3 of 100 rows" in contamination.found
    assert "##########" in contamination.detail
    assert contamination.evidence["contamination"] == pytest.approx(0.03, abs=1e-6)


def test_the_flagship_reports_two_findings_not_five(profile_fixture, baseline_contract):
    """One problem, one story.

    The contaminated column also breaks its format clause, and cascade
    suppression is what keeps that from tripling the noise.
    """
    contract = baseline_contract("clean_orders.csv")
    broken = profile_fixture("broken_dates.csv", as_name="clean_orders")

    signup = [v for v in check(broken, contract).violations if v.column == "signup_date"]

    assert {v.type for v in signup} == {
        ViolationType.TYPE_CONTAMINATED,
        ViolationType.EPOCH_SUSPECTED,
    }
    assert not any(v.type is ViolationType.FORMAT_CHANGED for v in signup)


def test_a_single_contaminated_row_is_enough(profile_fixture, baseline_contract):
    """Sensitivity is the point: three rows in a hundred, or one in a thousand."""
    contract = baseline_contract("clean_orders.csv")
    broken = profile_fixture("broken_dates.csv", as_name="clean_orders")
    report = check(broken, contract)
    violation = report.of_type(ViolationType.TYPE_CONTAMINATED)[0]
    assert violation.evidence["contamination"] < 0.05  # a rounding error's worth
    assert violation.severity.value == "fail"


# ── 2. a new category from an upstream release ────────────────────────────────


def test_a_new_status_value_is_caught_and_named(profile_fixture, baseline_contract):
    contract = baseline_contract("clean_orders.csv")
    drifted = profile_fixture("enum_drift.csv", as_name="clean_orders")

    report = check(drifted, contract)

    assert report.has(ViolationType.NEW_CATEGORY, "status")
    violation = report.of_type(ViolationType.NEW_CATEGORY)[0]
    assert violation.evidence["new_categories"] == ["awaiting_pickup"]
    assert "awaiting_pickup" in violation.found
    assert "12 rows" in violation.found  # the count matters for triage


def test_a_category_merely_going_unused_only_warns(profile_fixture, baseline_contract):
    """Absence is usually a quiet window, not a break."""
    contract = baseline_contract("clean_orders.csv")
    narrow = profile_fixture("broken_dates.csv", as_name="clean_orders")

    report = check(narrow, contract)
    disappeared = report.of_type(ViolationType.CATEGORY_DISAPPEARED)

    assert disappeared, "a vanished category should still be reported"
    assert disappeared[0].severity.value == "warn"


# ── 3. a unit change with no type change ──────────────────────────────────────


def test_dollars_becoming_cents_is_caught_as_a_unit_shift(profile_fixture, duck):
    """Nothing structural changes here: same type, no nulls, no new categories.

    Only the magnitude moves, which is why an approved range envelope is the
    only clause that can catch it.
    """
    from helpers import fixture_path
    from zeyvor.engines.base import Relation
    from zeyvor.profile import Profiler

    path = fixture_path("unit_shift.csv").replace("'", "''")
    pre_shift = Relation(
        sql=(
            f"(SELECT * FROM read_csv_auto('{path}', all_varchar=true) "
            "WHERE CAST(payment_id AS BIGINT) < 5080)"
        ),
        name="unit_shift",
        source_uri="pre-shift",
    )
    baseline = Profiler(duck).profile(pre_shift)
    contract = generate_contract(baseline)

    after = profile_fixture("unit_shift.csv")
    report = check(after, contract)

    assert report.has(ViolationType.UNIT_SHIFT_SUSPECTED, "amount")
    violation = report.of_type(ViolationType.UNIT_SHIFT_SUSPECTED)[0]
    assert violation.evidence["factor"] >= 10
    assert "unit changed" in violation.detail
    # The plain range breach is suppressed: same evidence, worse explanation.
    assert not report.has(ViolationType.RANGE_EXCEEDED, "amount")
    # And crucially, no type violation — the type is exactly what it was.
    assert not report.has(ViolationType.TYPE_CHANGED, "amount")


# ── 3b. a zero-padded code read as a number ───────────────────────────────────


def test_losing_leading_zeros_breaks_the_contract(profile_fixture, baseline_contract):
    """`00123` becoming `123` is what happens when a padded code meets `int`.

    This passed clean for a long time. `_should_pin_format` excluded numeric
    columns outright — right for auto-increment ids, whose digit count grows,
    and wrong for padded codes, whose width is fixed by the padding. So the
    contract said `type: integer` with no format clause, and the one property
    that mattered was asserted nowhere. The profiler had measured
    `leading_zeros` all along; `known_issues` recorded it and nothing checked it.
    """
    contract = baseline_contract("messy.csv")
    account_code = contract.tables["messy"].columns["account_code"]
    assert account_code.formats == ["#####"], "the padded width must be pinned"

    stripped = profile_fixture("unpadded_codes.csv", as_name="messy")
    report = check(stripped, contract)

    assert not report.ok
    assert report.has(ViolationType.FORMAT_CHANGED, "account_code")


def test_an_unpadded_id_column_still_gets_no_format_clause(profile_fixture):
    """The exclusion this narrows exists for a good reason — keep it working.

    `order_id` counts upward and its digit count will grow, so pinning `####`
    would schedule a failure for the day it reaches 10,000.
    """
    contract = generate_contract(profile_fixture("clean_orders.csv"))
    assert contract.tables["clean_orders"].columns["order_id"].formats == []


# ── 4. structural change ──────────────────────────────────────────────────────


def test_a_missing_column_is_caught(profile_fixture, baseline_contract):
    contract = baseline_contract("clean_orders.csv", tolerant=False)
    fewer = profile_fixture("broken_dates.csv", as_name="clean_orders")

    report = check(fewer, contract)
    missing = {v.column for v in report.of_type(ViolationType.COLUMN_MISSING)}

    assert {"customer_email", "amount", "country", "item_count"} <= missing


def test_a_new_column_is_allowed_by_default_and_can_be_forbidden(
    profile_fixture, baseline_contract
):
    """Most teams want to hear about a new column, not be blocked by it."""
    contract = baseline_contract("broken_dates.csv", table_name="t")
    wider = profile_fixture("clean_orders.csv", as_name="t")

    assert not check(wider, contract).has(ViolationType.COLUMN_ADDED)

    contract.tables["t"].allow_new_columns = False
    assert check(wider, contract).has(ViolationType.COLUMN_ADDED)


def test_a_table_the_profile_never_covered_is_reported(profile_fixture, baseline_contract):
    contract = baseline_contract("clean_orders.csv")
    unrelated = profile_fixture("messy.csv")

    report = check(unrelated, contract)

    assert report.has(ViolationType.TABLE_MISSING)
    assert report.tables_checked == 0


# ── 5. severity control ───────────────────────────────────────────────────────


def test_warn_only_mode_reports_everything_and_fails_nothing(profile_fixture, baseline_contract):
    """How a team adopts the tool without breaking their pipeline on day one."""
    from zeyvor.contract import Severity

    contract = baseline_contract("clean_orders.csv")
    contract.defaults.on_violation = Severity.WARN
    broken = profile_fixture("broken_dates.csv", as_name="clean_orders")

    report = check(broken, contract)

    assert report.violations, "violations should still be reported"
    assert report.failures == []
    assert report.exit_code == 0
    assert report.ok


def test_a_column_can_be_ignored_without_deleting_its_clause(profile_fixture, baseline_contract):
    contract = baseline_contract("clean_orders.csv")
    contract.tables["clean_orders"].columns["signup_date"].ignore = True
    broken = profile_fixture("broken_dates.csv", as_name="clean_orders")

    report = check(broken, contract)

    assert not any(v.column == "signup_date" for v in report.violations)
    # The clause is still in the file, so the intent stays visible in review.
    assert contract.tables["clean_orders"].columns["signup_date"].type == "date"


def test_a_single_column_can_be_downgraded_to_a_warning(profile_fixture, baseline_contract):
    from zeyvor.contract import Severity

    contract = baseline_contract("clean_orders.csv")
    contract.tables["clean_orders"].columns["signup_date"].on_violation = Severity.WARN
    broken = profile_fixture("broken_dates.csv", as_name="clean_orders")

    report = check(broken, contract)

    assert report.exit_code == 0
    assert any(v.column == "signup_date" for v in report.warnings)


# ── 6. reporting ──────────────────────────────────────────────────────────────


def test_a_clean_report_says_so_plainly(profile_fixture):
    profile = profile_fixture("clean_orders.csv")
    report = check(profile, generate_contract(profile))
    rendered = report.render()

    assert rendered.startswith("✔")
    assert "7 columns" in rendered


def test_findings_for_one_column_are_rendered_together(profile_fixture, baseline_contract):
    contract = baseline_contract("clean_orders.csv")
    broken = profile_fixture("broken_dates.csv", as_name="clean_orders")
    rendered = check(broken, contract).render()

    first = rendered.index("signup_date — type_contaminated")
    second = rendered.index("signup_date — epoch_suspected")
    status = rendered.index("status — category_disappeared")
    assert first < second < status, "a column's findings should not be interleaved"


def test_a_report_serialises_for_the_dashboard(profile_fixture, baseline_contract):
    contract = baseline_contract("clean_orders.csv")
    broken = profile_fixture("broken_dates.csv", as_name="clean_orders")
    data = check(broken, contract).to_dict()

    assert data["ok"] is False
    assert data["failed"] == 2
    assert data["warned"] == 1
    assert {v["type"] for v in data["violations"]} >= {"type_contaminated", "epoch_suspected"}
    assert all("severity" in v for v in data["violations"])
