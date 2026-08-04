"""Pattern tests run through DuckDB, not Python's `re`.

Executing them in the engine verifies two things at once: that the pattern means
what it should, and that it is RE2-compatible — which is what makes the same
expression valid on BigQuery too.
"""

from __future__ import annotations

import pytest

from zeyvor.engines.base import DuckDBDialect
from zeyvor.profile.patterns import ALL_PATTERNS, PATTERNS_BY_KEY, PII_KEYS, shape_expression

DIALECT = DuckDBDialect()

# (pattern key, value, expected match)
CASES = [
    # ── the flagship discriminator ────────────────────────────────────────────
    ("epoch_seconds", "1714089600", True),
    ("epoch_seconds", "5551234567", False),  # 10-digit phone must not match
    ("epoch_seconds", "171408960", False),  # 9 digits
    ("epoch_seconds", "2024-03-11", False),
    ("epoch_millis", "1714089600000", True),
    ("epoch_millis", "1714089600", False),
    # ── Excel serials ─────────────────────────────────────────────────────────
    ("excel_serial", "45231", True),
    ("excel_serial", "12345", False),  # out of the 20000-59999 window
    ("excel_serial", "4523", False),
    # ── date formats ──────────────────────────────────────────────────────────
    ("iso_date", "2024-03-11", True),
    ("iso_date", "2024-03-11T09:00:00", False),
    ("iso_datetime", "2024-03-11T09:00:00", True),
    ("iso_datetime", "2024-03-11 09:00", True),
    ("us_date", "3/11/2024", True),
    ("us_date", "03/11/2024", True),
    ("us_date", "2024-03-11", False),
    ("eu_date", "11.03.2024", True),
    ("year_only", "2024", True),
    ("year_only", "1899", False),
    # ── numbers in disguise ───────────────────────────────────────────────────
    ("currency", "$1,299.00", True),
    ("currency", "£45", True),
    ("currency", "1299", False),
    ("percent", "12.5%", True),
    ("percent", "12.5", False),
    ("thousands_separated", "1,299,000", True),
    ("thousands_separated", "1299000", False),
    ("scientific", "1.2e6", True),
    ("leading_zeros", "00123", True),
    ("leading_zeros", "123", False),
    ("leading_zeros", "0", False),
    # ── structure ─────────────────────────────────────────────────────────────
    ("uuid", "3f2504e0-4f89-11d3-9a0c-0305e82c3301", True),
    ("uuid", "3f2504e0-4f89-11d3-9a0c", False),
    ("json_object", '{"a": 1}', True),
    ("json_object", "[1, 2, 3]", True),
    ("json_object", "plain text", False),
    ("url", "https://zeyvor.com/docs", True),
    ("url", "zeyvor.com", False),
    ("null_word", "N/A", True),  # lower-cased before matching
    ("null_word", "NULL", True),
    ("null_word", "-", True),
    ("null_word", "nothing", False),
    ("mojibake", "cafÃ©", True),
    ("mojibake", "café", False),
    # ── PII ───────────────────────────────────────────────────────────────────
    ("email", "ada@example.com", True),
    ("email", "ada@example", False),
    ("email", "contact ada@example.com now", False),  # full match only
    ("email_embedded", "contact ada@example.com now", True),
    ("email_embedded", "no address here", False),
    ("phone_formatted", "555-123-4567", True),
    ("phone_formatted", "(555) 123-4567", True),
    ("phone_formatted", "555.123.4567", True),
    ("phone_formatted", "11.03.2024", False),  # a date is not a phone
    ("phone_formatted", "1714089600", False),  # nor is a timestamp
    ("phone_international", "+44 20 7123 4567", True),
    ("phone_international", "5551234567", False),
    ("ssn", "123-45-6789", True),
    ("credit_card", "4111 1111 1111 1111", True),
    ("credit_card", "4111111111111111", False),  # separators required
    ("ipv4", "192.168.1.1", True),
    ("iban", "GB29NWBK60161331926819", True),
    # postal_like is recorded but must never be classed as PII
    ("postal_like", "94107", True),
    ("postal_like", "94107-1234", True),
]


@pytest.mark.parametrize("key,value,expected", CASES)
def test_pattern_matches(duck, key, value, expected):
    pattern = PATTERNS_BY_KEY[key]
    literal = DIALECT.quote_literal(value)
    subject = DIALECT.lower(literal) if pattern.lowercase else literal
    sql = f"SELECT {DIALECT.regex_match(subject, pattern.regex)}"
    (result,) = duck.execute_one(sql)
    assert bool(result) is expected, f"{key} on {value!r} should be {expected}"


def test_every_pattern_is_re2_compatible(duck):
    """A pattern that DuckDB cannot compile would also fail on BigQuery."""
    for pattern in ALL_PATTERNS:
        sql = f"SELECT {DIALECT.regex_match(DIALECT.quote_literal('probe'), pattern.regex)}"
        duck.execute_one(sql)  # raises EngineError if the regex is invalid


def test_postal_like_is_not_pii():
    """Five digits is not evidence of a postal code — it is evidence of digits."""
    assert "postal_like" not in PII_KEYS


def test_pii_keys_are_the_expected_set():
    assert {
        "email",
        "email_embedded",
        "phone_formatted",
        "phone_international",
        "ssn",
        "credit_card",
        "ipv4",
        "iban",
    } == PII_KEYS


SHAPE_CASES = [
    ("2024-03-11", "####-##-##"),
    ("1714089600", "##########"),
    ("$1,299.00", "$#,###.##"),
    ("Alice Smith", "aaaaa_aaaaa"),
    ("ABC-123", "aaa-###"),
    ("", ""),
]


@pytest.mark.parametrize("value,expected", SHAPE_CASES)
def test_shape_expression(duck, value, expected):
    sql = f"SELECT {shape_expression(DIALECT, DIALECT.quote_literal(value))}"
    (result,) = duck.execute_one(sql)
    assert result == expected


def test_shape_distinguishes_dates_from_timestamps(duck):
    """The whole point: a format change is visible without seeing a value."""
    date_sql = f"SELECT {shape_expression(DIALECT, DIALECT.quote_literal('2024-03-11'))}"
    epoch_sql = f"SELECT {shape_expression(DIALECT, DIALECT.quote_literal('1714089600'))}"
    assert duck.execute_one(date_sql)[0] != duck.execute_one(epoch_sql)[0]
