"""Generation rules.

Everything here is one question asked repeatedly: is this clause supported by
evidence, or is it a guess that will fail on ordinary Tuesday data? Several of
these tests exist because the first implementation guessed, and the self-check
caught it.
"""

from __future__ import annotations

import pytest

from zeyvor.contract import RangePolicy, generate_contract, generate_column_contract
from zeyvor.contract.generate import _pad_lower, _pad_upper, _round_up_1sf


# ── range padding ─────────────────────────────────────────────────────────────


def test_rounding_to_one_significant_figure():
    assert _round_up_1sf(733) == 800
    assert _round_up_1sf(0.42) == pytest.approx(0.5)
    assert _round_up_1sf(1) == 1
    assert _round_up_1sf(0) == 0


def test_upper_bound_leaves_room_for_growth_but_not_for_a_unit_change():
    """The whole padding policy in one assertion.

    Doubling absorbs ordinary growth; a hundredfold jump stays far outside.
    """
    padded = _pad_upper(366.49, RangePolicy())
    assert padded > 366.49 * 1.5, "ordinary growth must not trip the bound"
    assert padded < 366.49 * 100, "a unit change must still be caught"


def test_lower_bound_uses_zero_for_non_negative_data():
    """A negative price is a real signal, and zero is a bound anyone understands."""
    assert _pad_lower(19.99, RangePolicy()) == 0


def test_lower_bound_is_widened_when_negatives_are_normal():
    assert _pad_lower(-40.0, RangePolicy()) <= -80


def test_padding_is_configurable():
    generous = RangePolicy(numeric_headroom=10.0)
    assert _pad_upper(100, generous) > _pad_upper(100, RangePolicy())


# ── clauses that must not be guessed ──────────────────────────────────────────


def test_uniqueness_is_only_asserted_for_key_like_columns(profile_fixture):
    """100 distinct prices in 100 rows proves nothing about the 101st."""
    contract = generate_contract(profile_fixture("clean_orders.csv"))
    columns = contract.tables["clean_orders"].columns

    assert columns["order_id"].unique is True
    assert columns["customer_email"].unique is None
    assert columns["amount"].unique is None


def test_numbers_never_get_a_format_clause(profile_fixture):
    """A digit count is not a format: '####' fails the day ids reach 10,000."""
    contract = generate_contract(profile_fixture("clean_orders.csv"))
    columns = contract.tables["clean_orders"].columns

    assert columns["order_id"].formats == []
    assert columns["amount"].formats == []
    assert columns["signup_date"].formats == ["####-##-##"]


def test_identifiers_never_get_a_range_clause(profile_fixture):
    """An auto-incrementing id grows past every ceiling; uniqueness is the
    assertion that actually means something for a key."""
    contract = generate_contract(profile_fixture("clean_orders.csv"))
    order_id = contract.tables["clean_orders"].columns["order_id"]

    assert order_id.minimum is None
    assert order_id.maximum is None
    assert order_id.unique is True


def test_variable_length_text_gets_no_format_clause(profile_fixture):
    """Email shapes are incidental — they change with every new address."""
    contract = generate_contract(profile_fixture("clean_orders.csv"))
    assert contract.tables["clean_orders"].columns["customer_email"].formats == []


def test_fixed_width_codes_do_get_a_format_clause(profile_fixture):
    contract = generate_contract(profile_fixture("clean_orders.csv"))
    assert contract.tables["clean_orders"].columns["country"].formats == ["aa"]


def test_numeric_columns_never_get_closed_categories(profile_fixture):
    """item_count holding 0-6 today will hold 7 tomorrow."""
    contract = generate_contract(profile_fixture("clean_orders.csv"))
    item_count = contract.tables["clean_orders"].columns["item_count"]

    assert item_count.categories is None
    assert item_count.categories_closed is False
    # The useful assertion for a count is a range, and that is present.
    assert item_count.maximum is not None


def test_text_categories_are_closed_when_the_set_is_complete(profile_fixture):
    contract = generate_contract(profile_fixture("clean_orders.csv"))
    status = contract.tables["clean_orders"].columns["status"]

    assert status.categories == ["delivered", "pending", "refunded", "shipped"]
    assert status.categories_closed is True


def test_open_world_columns_are_never_closed(profile_fixture):
    """Found by running against real data.

    A `phone` column with twenty distinct values had its set closed, which both
    enshrined 'notanumber' as required vocabulary and guaranteed a failure on
    the next legitimate number. Names, places and contact details are never a
    closed vocabulary, however few values today's sample holds.
    """
    contract = generate_contract(profile_fixture("messy.csv"))
    columns = contract.tables["messy"].columns

    assert columns["full_name"].categories_closed is False
    assert columns["account_code"].categories_closed is False


def test_a_vocabulary_needs_real_repetition(profile_fixture):
    """A set of values each seen once or twice is a list, not a vocabulary."""
    from zeyvor.profile.models import ColumnProfile, EnumMember, EnumProfile, InferredType
    from zeyvor.profile.types import finalise

    def column_with(distinct: int, rows: int):
        col = ColumnProfile(
            name="label",
            row_count=rows,
            distinct_count=distinct,
            type_probes={},
            enum=EnumProfile(
                members=[EnumMember(f"v{i}", rows // distinct) for i in range(distinct)],
                complete=True,
                cardinality=distinct,
            ),
        )
        return finalise(col)

    # 4 values across 100 rows: heavily repeated, a genuine vocabulary.
    assert generate_column_contract(column_with(4, 100)).categories_closed is True
    # 40 values across 100 rows: mostly one-offs, so left open.
    assert generate_column_contract(column_with(40, 100)).categories_closed is False


def test_numbers_in_disguise_are_not_a_vocabulary(profile_fixture):
    """'$150.00' infers as text, but prices are as open-world as floats."""
    profile = profile_fixture("messy.csv")
    assert profile.column("revenue").has("currency_in_text"), "fixture precondition"

    contract = generate_contract(profile)
    assert contract.tables["messy"].columns["revenue"].categories_closed is False


def test_an_incomplete_category_set_is_never_closed(profile_fixture):
    from zeyvor.profile import ProfileOptions

    profile = profile_fixture("clean_orders.csv", ProfileOptions(max_enum_cardinality=2))
    contract = generate_contract(profile)
    assert contract.tables["clean_orders"].columns["status"].categories is None


def test_hashed_categories_are_never_closed(profile_fixture):
    """Under strict privacy the members are digests, so membership is unusable."""
    from zeyvor.profile import ProfileOptions

    profile = profile_fixture("clean_orders.csv", ProfileOptions(privacy="strict"))
    contract = generate_contract(profile)
    assert contract.tables["clean_orders"].columns["status"].categories is None


# ── presence ──────────────────────────────────────────────────────────────────


def test_a_column_with_no_nulls_is_contracted_non_nullable(profile_fixture):
    contract = generate_contract(profile_fixture("clean_orders.csv"))
    assert contract.tables["clean_orders"].columns["order_id"].nullable is False


def test_a_column_with_nulls_gets_a_tolerance_not_a_prohibition(profile_fixture):
    """Otherwise the contract fails on exactly the data it was written from."""
    profile = profile_fixture("messy.csv")
    contract = generate_contract(profile)
    column = contract.tables["messy"].columns["signup_date"]

    if profile.column("signup_date").null_count:
        assert column.nullable is True
        assert column.max_null_rate is not None
        assert column.max_null_rate > (profile.column("signup_date").null_rate or 0)


def test_an_empty_column_promises_nothing(profile_fixture):
    contract = generate_contract(profile_fixture("edge_cases.csv"))
    column = contract.tables["edge_cases"].columns["always_null"]

    assert column.type is None
    assert column.nullable is None
    assert column.formats == []


# ── privacy ───────────────────────────────────────────────────────────────────


def test_no_pii_is_asserted_only_where_none_was_seen(profile_fixture):
    contract = generate_contract(profile_fixture("clean_orders.csv"))
    columns = contract.tables["clean_orders"].columns

    assert columns["status"].no_pii is True
    # The email column holds PII by design, so the clause would be nonsense.
    assert columns["customer_email"].no_pii is False


def test_no_pii_is_withheld_from_columns_whose_name_implies_personal_data(profile_fixture):
    """A `phone` column that happens to look clean today should not be
    contracted as PII-free — the name says what it is for."""
    contract = generate_contract(profile_fixture("messy.csv"))
    assert contract.tables["messy"].columns["full_name"].no_pii is False


def test_known_issues_record_accepted_defects(profile_fixture):
    contract = generate_contract(profile_fixture("messy.csv"))
    columns = contract.tables["messy"].columns

    assert "mojibake" in columns["notes"].known_issues
    assert "null_words" in columns["signup_date"].known_issues
    assert "leading_zeros" in columns["account_code"].known_issues


# ── table level ───────────────────────────────────────────────────────────────


def test_provenance_is_recorded(profile_fixture):
    profile = profile_fixture("clean_orders.csv")
    contract = generate_contract(profile)
    table = contract.tables["clean_orders"]

    assert table.source == profile.source_uri
    assert table.profile_fingerprint == profile.fingerprint()
    assert contract.generated_by.startswith("zeyvor ")
    assert contract.generated_at


def test_several_tables_in_one_contract(profile_fixture):
    contract = generate_contract(
        [profile_fixture("clean_orders.csv"), profile_fixture("messy.csv")]
    )
    assert set(contract.tables) == {"clean_orders", "messy"}


# ── the describer hook ────────────────────────────────────────────────────────


def test_a_describer_supplies_the_prose(profile_fixture):
    def describer(profile, table):
        return {name: f"Description of {name}." for name in table.columns}

    contract = generate_contract(profile_fixture("clean_orders.csv"), describer=describer)
    assert contract.tables["clean_orders"].columns["status"].means == "Description of status."


def test_a_failing_describer_still_yields_a_usable_contract(profile_fixture):
    """An undocumented contract works; a missing contract does not."""

    def broken(profile, table):
        raise RuntimeError("no API key")

    contract = generate_contract(profile_fixture("clean_orders.csv"), describer=broken)
    columns = contract.tables["clean_orders"].columns

    assert columns["status"].categories_closed is True   # clauses intact
    assert columns["status"].means is None               # prose absent
    assert "descriptions unavailable" in contract.generated_by


def test_generation_needs_no_network_or_key(profile_fixture):
    """The default path must never require credentials."""
    contract = generate_contract(profile_fixture("clean_orders.csv"))
    assert contract.tables["clean_orders"].columns
