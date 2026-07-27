"""The specification, written as tests.

Each test below is a real production failure that passes every conventional data
check — no nulls introduced, no duplicates, no row-count anomaly, no type error
at read time — and is caught here anyway. If Zeyvor exists for one reason, this
file is it.
"""

from __future__ import annotations

from zeyvor.profile import InferredType, Observation, ProfileOptions

# ── 1. A date column starts receiving Unix timestamps ─────────────────────────


def test_epoch_timestamps_arriving_in_a_date_column(profile_fixture):
    """The flagship failure.

    An upstream API changes and `signup_date` begins arriving as
    `1714089600`. The column is still complete, still unique-ish, still the
    expected row count. Every dashboard filtered by date is now wrong, and
    nothing anywhere goes red.
    """
    profile = profile_fixture("broken_dates.csv")
    column = profile.column("signup_date")

    # The break is visible three ways over.
    assert column.has(Observation.EPOCH_SUSPECTED)
    assert column.has(Observation.MIXED_TYPES)
    assert column.inferred_type is InferredType.MIXED

    # Proportions are exact, which is what makes the finding actionable.
    assert column.type_mixture["date"] == 0.97
    assert column.type_mixture["integer"] == 0.03

    # And the evidence needs no values: two shapes, one of them new.
    shapes = {s.shape: s.count for s in column.shapes}
    assert shapes["####-##-##"] == 97
    assert shapes["##########"] == 3

    # Meanwhile every conventional signal is clean.
    assert column.null_count == 0
    assert column.blank_count == 0
    assert profile.row_count == 100


def test_a_date_column_that_flipped_entirely_to_excel_serials(profile_fixture):
    """The same failure via a spreadsheet round-trip instead of an API."""
    profile = profile_fixture("excel_serials.csv")
    column = profile.column("issued_date")
    assert column.has(Observation.EXCEL_SERIAL_SUSPECTED)
    assert column.inferred_type is InferredType.INTEGER


# ── 2. PII leaking into a column nobody is watching ───────────────────────────


def test_personal_data_inside_a_free_text_column(profile_fixture):
    """No name-based checker can see this.

    The column is called `notes`. Nothing about the name suggests personal
    data, and support agents have been pasting customer email addresses into
    it for months. Detection has to come from content.
    """
    profile = profile_fixture("messy.csv")
    notes = profile.column("notes")

    assert notes.has(Observation.PII_IN_FREE_TEXT)
    assert notes.has(Observation.PII_DETECTED)
    assert "email_embedded" in notes.pii_signals

    # An actual email column is a different finding: expected, not a leak.
    clean = profile_fixture("clean_orders.csv")
    email = clean.column("customer_email")
    assert email.has(Observation.PII_DETECTED)
    assert not email.has(Observation.PII_IN_FREE_TEXT)


def test_five_digit_numbers_are_not_reported_as_postal_codes(profile_fixture):
    """Precision matters as much as recall.

    `account_code` holds zero-padded five-digit codes. Treating those as
    postal codes would put a PII finding on a large share of every warehouse
    and train users to ignore the tool.
    """
    profile = profile_fixture("messy.csv")
    account_code = profile.column("account_code")
    assert not account_code.has(Observation.PII_DETECTED)
    assert account_code.pii_signals == []


# ── 3. Identifiers silently destroyed by a type cast ──────────────────────────


def test_leading_zeros_that_an_integer_cast_would_erase(profile_fixture):
    """`00123` becomes `123` and joins start failing on a subset of rows.

    The column looks numeric to any profiler that only checks castability, so
    the fix everyone reaches for — "store it as an int" — is the bug.
    """
    profile = profile_fixture("messy.csv")
    column = profile.column("account_code")
    assert column.has(Observation.LEADING_ZEROS)
    assert column.has(Observation.NUMERIC_STORED_AS_TEXT)
    assert column.dominant_shape.shape == "#####"


# ── 4. Numbers that are not numbers ───────────────────────────────────────────


def test_money_and_percentages_stored_as_text(profile_fixture):
    """`SUM(revenue)` returns zero and no one notices for a quarter."""
    profile = profile_fixture("messy.csv")

    revenue = profile.column("revenue")
    assert revenue.has(Observation.CURRENCY_IN_TEXT)
    assert revenue.dominant_shape.shape == "$#,###.##"

    growth = profile.column("growth_rate")
    assert growth.has(Observation.PERCENT_IN_TEXT)


# ── 5. Formats drifting inside a single column ────────────────────────────────


def test_several_date_formats_in_one_column(profile_fixture):
    """Two upstream systems, two conventions, one column.

    11/03 is March 11th to one of them and November 3rd to the other, and the
    rows that parse are exactly as dangerous as the rows that do not.
    """
    profile = profile_fixture("messy.csv")
    column = profile.column("signup_date")
    assert column.has(Observation.MULTIPLE_DATE_FORMATS)
    assert column.has(Observation.MIXED_TYPES)
    assert len(column.shapes) >= 3


def test_dates_in_a_format_no_engine_will_parse(profile_fixture):
    """A column of `3/11/2024` is temporal, whatever the SQL engine says.

    DuckDB refuses to cast it, so a cast-only profiler would write the column
    off as free text and never generate a date contract for it at all.
    """
    profile = profile_fixture("us_dates.csv")
    column = profile.column("closed_date")
    assert column.inferred_type is InferredType.DATE
    assert column.type_mixture.get("date") == 1.0
    assert column.pattern_hits.get("us_date") == 50


# ── 6. Text standing in for missing data ──────────────────────────────────────


def test_null_words_masquerading_as_values(profile_fixture):
    """`N/A`, `null` and `-` are missing data that every null check counts as present."""
    profile = profile_fixture("messy.csv")
    column = profile.column("signup_date")
    assert column.has(Observation.NULL_WORDS)
    assert column.null_count == 0  # the point: SQL sees nothing missing


# ── 7. Encoding and whitespace damage ─────────────────────────────────────────


def test_broken_character_encoding(profile_fixture):
    profile = profile_fixture("messy.csv")
    assert profile.column("notes").has(Observation.MOJIBAKE)


def test_padding_and_casing_that_break_grouping(profile_fixture):
    """' Alice Smith ' and 'Alice Smith' are two customers to a GROUP BY."""
    profile = profile_fixture("messy.csv")
    assert profile.column("full_name").has(Observation.WHITESPACE_PADDING)
    assert profile.column("status").has(Observation.INCONSISTENT_CASE)


# ── 8. Categories: the ground truth a contract needs ──────────────────────────


def test_category_sets_are_captured_completely(profile_fixture):
    """Detecting a *new* category later is only possible if the old set is known.

    This is the measurement that makes enum drift detectable in Part 2, and it
    is why the recorded set carries an explicit completeness flag.
    """
    baseline = profile_fixture("clean_orders.csv")
    status = baseline.column("status")
    assert status.enum.complete
    assert set(status.enum.values()) == {"pending", "shipped", "delivered", "refunded"}

    # The same column after an upstream release adds a state.
    drifted = profile_fixture("enum_drift.csv")
    assert "awaiting_pickup" in drifted.column("status").enum.values()
    assert set(drifted.column("status").enum.values()) - set(status.enum.values()) == {
        "awaiting_pickup"
    }


# ── 9. A unit change with no type change ──────────────────────────────────────


def test_a_currency_column_that_switched_to_cents(profile_fixture):
    """Types hold, nulls hold, only the magnitude moves.

    Nothing about this is detectable from the schema. It shows up as a
    distribution whose maximum has left its own interquartile range far behind,
    which is precisely what a range clause in a contract is for.
    """
    profile = profile_fixture("unit_shift.csv")
    stats = profile.column("amount").numeric
    assert stats is not None
    assert stats.maximum > stats.p75 * 10
    assert profile.column("amount").null_count == 0


# ── 10. Privacy is not traded against accuracy ────────────────────────────────


def test_strict_privacy_finds_exactly_the_same_problems(profile_fixture):
    """The safest mode must not be the weakest one.

    If turning privacy up cost detections, every serious user would be pushed
    towards the mode that leaks — so the two must agree exactly.
    """
    strict = profile_fixture("messy.csv", ProfileOptions(privacy="strict"))
    masked = profile_fixture("messy.csv", ProfileOptions(privacy="masked"))

    for a, b in zip(strict.columns, masked.columns, strict=True):
        assert a.name == b.name
        assert a.observations == b.observations
        assert a.inferred_type is b.inferred_type
        assert a.pattern_hits == b.pattern_hits


def test_strict_privacy_emits_no_recognisable_values(profile_fixture):
    profile = profile_fixture("clean_orders.csv", ProfileOptions(privacy="strict"))
    serialised = profile.to_json()
    assert "shipped" not in serialised
    assert "@example.com" not in serialised
    # Shapes and counts survive, which is all the diff engine needs.
    assert "####-##-##" in serialised
