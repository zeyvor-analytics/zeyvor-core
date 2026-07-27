"""Privacy rules — the guarantees the product's positioning depends on."""

from __future__ import annotations

import pytest

from zeyvor.profile.privacy import (
    DEFAULT_MODE,
    PrivacyMode,
    apply_to_enum_value,
    coerce_mode,
    collects_samples,
    hash_value,
    mask_value,
    sample_limit,
)


def test_the_default_is_not_the_permissive_mode():
    assert DEFAULT_MODE is PrivacyMode.MASKED
    assert not collects_samples(DEFAULT_MODE)


def test_mode_coercion():
    assert coerce_mode(None) is DEFAULT_MODE
    assert coerce_mode("strict") is PrivacyMode.STRICT
    assert coerce_mode("FULL") is PrivacyMode.FULL
    assert coerce_mode(PrivacyMode.MASKED) is PrivacyMode.MASKED


def test_unknown_mode_fails_loudly_with_the_valid_options():
    with pytest.raises(ValueError) as excinfo:
        coerce_mode("relaxed")
    message = str(excinfo.value)
    assert "relaxed" in message and "strict" in message and "masked" in message


def test_only_full_mode_collects_samples():
    assert sample_limit(PrivacyMode.STRICT) == 0
    assert sample_limit(PrivacyMode.MASKED) == 0
    assert sample_limit(PrivacyMode.FULL) > 0


def test_hashes_are_reproducible_across_runs():
    """A contract compares today's categories with last month's.

    That only works if the digest is deterministic, which is why there is no
    per-run salt.
    """
    assert hash_value("shipped") == hash_value("shipped")
    assert hash_value("shipped") != hash_value("pending")
    assert hash_value("shipped").startswith("sha256:")


def test_hashes_do_not_leak_the_input():
    assert "shipped" not in hash_value("shipped")


def test_masking_preserves_structure_not_content():
    assert mask_value("ada@example.com") == "aaa@aaaaaaa.aaa"
    assert mask_value("2024-03-11") == "####-##-##"
    assert mask_value("$1,299.00") == "$#,###.##"
    assert mask_value("Alice Smith") == "aaaaa_aaaaa"


def test_masking_is_length_bounded():
    assert len(mask_value("x" * 500)) <= 64


def test_enum_values_are_hashed_only_in_strict_mode():
    assert apply_to_enum_value("shipped", PrivacyMode.STRICT).startswith("sha256:")
    assert apply_to_enum_value("shipped", PrivacyMode.MASKED) == "shipped"
    assert apply_to_enum_value("shipped", PrivacyMode.FULL) == "shipped"
