"""The pattern library: shape-level evidence about what values really are.

Two jobs.

**Format patterns** tell us what a value looks like independent of its declared
type. This is how the flagship failure gets caught: a column contracted as
calendar dates that starts arriving as ``1714089600`` scores 0% on ``iso_date``
and 100% on ``epoch_seconds`` — a conclusion reached from counts alone, without
a single raw value leaving the machine.

**PII patterns** count personal data by content rather than by column name. The
column called ``notes`` holding 47 email addresses is invisible to any
name-based check, and it is exactly the leak that matters.

All patterns are RE2-compatible (no lookaheads, no backreferences) so the same
expression runs on DuckDB, BigQuery and Snowflake.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Pattern:
    key: str
    regex: str
    description: str
    category: str = "format"
    is_pii: bool = False
    lowercase: bool = False
    """When true the value is lower-cased before matching (dialect-portable
    substitute for an inline case-insensitivity flag)."""


# ── Temporal formats ──────────────────────────────────────────────────────────

TEMPORAL = [
    Pattern("iso_date", r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$", "ISO calendar date (2024-03-11)"),
    Pattern(
        "iso_datetime",
        r"^[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}",
        "ISO timestamp (2024-03-11T14:20:00)",
    ),
    Pattern("us_date", r"^[0-9]{1,2}/[0-9]{1,2}/[0-9]{4}$", "US-style date (3/11/2024)"),
    Pattern("eu_date", r"^[0-9]{1,2}\.[0-9]{1,2}\.[0-9]{4}$", "European date (11.03.2024)"),
    Pattern("compact_date", r"^[0-9]{8}$", "Compact date or 8-digit code (20240311)"),
    # A 10-digit integer beginning with 1 covers 2001-2033 in Unix seconds. North
    # American phone numbers never start with 1, so this discriminates cleanly
    # between "epoch timestamp" and "phone number stored as digits".
    Pattern("epoch_seconds", r"^1[0-9]{9}$", "Unix timestamp in seconds (1714089600)"),
    Pattern("epoch_millis", r"^1[0-9]{12}$", "Unix timestamp in milliseconds"),
    # 20000-59999 spans roughly 1954-2064 as an Excel serial date.
    Pattern("excel_serial", r"^[2-5][0-9]{4}$", "Excel serial date (45231)"),
    Pattern("year_only", r"^(19|20)[0-9]{2}$", "Bare four-digit year"),
]

# ── Numeric formats hiding inside text ────────────────────────────────────────

NUMERIC_TEXT = [
    Pattern(
        "currency",
        r"^[-+]?[$€£¥]\s?[0-9][0-9,]*(\.[0-9]+)?$",
        "Money with a currency symbol ($1,299.00)",
    ),
    Pattern("percent", r"^[-+]?[0-9]+(\.[0-9]+)?\s?%$", "Percentage as text (12.5%)"),
    Pattern(
        "thousands_separated",
        r"^[-+]?[0-9]{1,3}(,[0-9]{3})+(\.[0-9]+)?$",
        "Number with thousand separators (1,299,000)",
    ),
    Pattern(
        "scientific",
        r"^[-+]?[0-9]+(\.[0-9]+)?[eE][-+]?[0-9]+$",
        "Scientific notation (1.2e6)",
    ),
    # Losing these to an integer cast is a classic silent data-destroying bug.
    Pattern(
        "leading_zeros",
        r"^0[0-9]+$",
        "Digits with a significant leading zero (00123) — must stay text",
    ),
]

# ── Identifiers and structure ─────────────────────────────────────────────────

STRUCTURAL = [
    Pattern(
        "uuid",
        r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
        "UUID",
    ),
    Pattern("json_object", r"^\s*[{[][\s\S]*[}\]]\s*$", "Embedded JSON object or array"),
    Pattern("url", r"^https?://[^\s]+$", "URL"),
    Pattern(
        "null_word",
        r"^(null|none|n/a|na|nan|nil|undefined|missing|-|\?)$",
        "Text standing in for a missing value",
        lowercase=True,
    ),
    Pattern(
        "mojibake",
        r"(Â|â€|Ã¢|Ã©|Ã¨|ï»¿)",
        "Mis-decoded characters — an encoding step is broken",
    ),
    # Recorded but deliberately *not* classed as PII. Every five-digit number
    # matches it — quantities, order counts, zero-padded account codes — so the
    # shape is not evidence of a postal code on its own. Part 2 can promote it
    # once the column *name* agrees; doing so here would flag half a warehouse.
    Pattern(
        "postal_like",
        r"^[0-9]{5}(-[0-9]{4})?$",
        "Five digits, optionally +4 — postal-shaped, weak evidence alone",
    ),
]

# ── PII, detected by content ──────────────────────────────────────────────────

PII = [
    Pattern(
        "email",
        r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$",
        "Email address",
        category="pii",
        is_pii=True,
    ),
    Pattern(
        "email_embedded",
        r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}",
        "Email address inside a longer string",
        category="pii",
        is_pii=True,
    ),
    # Deliberately demanding. Two weaker formulations were tried and rejected:
    # bare digit strings (flags every ID and epoch timestamp) and "digits with
    # any separator" (flags 11.03.2024 as a phone number). Requiring an explicit
    # 3-3-4 grouping, or an international prefix, costs a little recall and buys
    # a great deal of precision.
    Pattern(
        "phone_formatted",
        r"^[(]?[0-9]{3}[)]?[ .\-][0-9]{3}[ .\-][0-9]{4}$",
        "Phone number in 3-3-4 grouping (555-123-4567)",
        category="pii",
        is_pii=True,
    ),
    Pattern(
        "phone_international",
        r"^[+][0-9]{1,3}[ .\-]?[0-9][0-9 .\-()]{5,16}[0-9]$",
        "Phone number with an international dialling prefix",
        category="pii",
        is_pii=True,
    ),
    Pattern("ssn", r"^[0-9]{3}-[0-9]{2}-[0-9]{4}$", "US SSN", category="pii", is_pii=True),
    Pattern(
        "credit_card",
        r"^([0-9]{4}[ -]){3}[0-9]{1,4}$",
        "Payment card number with separators",
        category="pii",
        is_pii=True,
    ),
    Pattern(
        "ipv4",
        r"^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$",
        "IPv4 address",
        category="pii",
        is_pii=True,
    ),
    Pattern(
        "iban",
        r"^[A-Z]{2}[0-9]{2}[A-Z0-9]{10,30}$",
        "IBAN bank account number",
        category="pii",
        is_pii=True,
    ),
]

ALL_PATTERNS: tuple[Pattern, ...] = tuple(TEMPORAL + NUMERIC_TEXT + STRUCTURAL + PII)

PATTERNS_BY_KEY: dict[str, Pattern] = {p.key: p for p in ALL_PATTERNS}

PII_KEYS: frozenset[str] = frozenset(p.key for p in ALL_PATTERNS if p.is_pii)


# ── Value shapes ──────────────────────────────────────────────────────────────

# Shapes are the privacy-preserving fingerprint of a column's formatting:
# digits collapse to '#', letters to 'a', whitespace to '_', punctuation stays.
# '2024-03-11' becomes '####-##-##'; '1714089600' becomes '##########'. Grouping
# by shape reveals a format change without revealing a single value.
SHAPE_DIGIT_CLASS = "[0-9]"
SHAPE_LETTER_CLASS = "[A-Za-z]"
SHAPE_SPACE_CLASS = r"\s"
SHAPE_MAX_LENGTH = 40


def shape_expression(dialect, expr: str) -> str:
    """SQL that converts a text expression into its shape signature."""
    truncated = dialect.substr(expr, 1, SHAPE_MAX_LENGTH)
    shaped = dialect.regex_replace_all(truncated, SHAPE_DIGIT_CLASS, "#")
    shaped = dialect.regex_replace_all(shaped, SHAPE_LETTER_CLASS, "a")
    shaped = dialect.regex_replace_all(shaped, SHAPE_SPACE_CLASS, "_")
    return shaped


def patterns_for(keys: "list[str] | None" = None) -> tuple[Pattern, ...]:
    if keys is None:
        return ALL_PATTERNS
    wanted = set(keys)
    return tuple(p for p in ALL_PATTERNS if p.key in wanted)


__all__ = [
    "Pattern",
    "ALL_PATTERNS",
    "PATTERNS_BY_KEY",
    "PII_KEYS",
    "TEMPORAL",
    "NUMERIC_TEXT",
    "STRUCTURAL",
    "PII",
    "shape_expression",
    "patterns_for",
    "SHAPE_MAX_LENGTH",
]
