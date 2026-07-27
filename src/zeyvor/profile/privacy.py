"""What may leave the machine.

This module exists because the whole product strategy rests on one sentence:
*your rows never leave your infrastructure.* A profile is meant to be safe to
commit to git, paste into a pull request, and send to a language model — so the
rules about which values can appear in it belong in one auditable place rather
than scattered through the SQL.

Structural guarantees, true in every mode:

* No row is ever fetched. Every number in a profile is a SQL aggregate.
* Minimum and maximum *values* of text columns are never collected — only
  lengths. Alphabetical extremes are real customer data, so the profiler simply
  never asks for them.
* Shapes (``####-##-##``) and pattern counts are derived, not values.

What varies by mode is the handling of low-cardinality value sets and samples.
"""

from __future__ import annotations

import hashlib
from enum import Enum


class PrivacyMode(str, Enum):
    STRICT = "strict"
    """Nothing recognisable leaves. Enum members are hashed; no samples."""

    MASKED = "masked"
    """Default. Enum members are kept as-is because a low-cardinality set is
    business vocabulary (``shipped``, ``refunded``, ``EU-WEST``) and a contract
    cannot detect a new category without knowing the old ones. No row samples."""

    FULL = "full"
    """Adds a handful of real example values per column. Useful locally when a
    human is reading the profile; never the default."""


DEFAULT_MODE = PrivacyMode.MASKED

# Sample values are only ever collected in FULL mode.
SAMPLE_LIMITS: dict[PrivacyMode, int] = {
    PrivacyMode.STRICT: 0,
    PrivacyMode.MASKED: 0,
    PrivacyMode.FULL: 5,
}


def coerce_mode(mode: "PrivacyMode | str | None") -> PrivacyMode:
    if mode is None:
        return DEFAULT_MODE
    if isinstance(mode, PrivacyMode):
        return mode
    try:
        return PrivacyMode(str(mode).lower())
    except ValueError as exc:
        valid = ", ".join(m.value for m in PrivacyMode)
        raise ValueError(f"Unknown privacy mode {mode!r}. Expected one of: {valid}") from exc


def sample_limit(mode: PrivacyMode) -> int:
    return SAMPLE_LIMITS[mode]


def collects_samples(mode: PrivacyMode) -> bool:
    return sample_limit(mode) > 0


def hash_value(value: str, *, length: int = 12) -> str:
    """Stable, salt-free digest so the same category hashes alike across runs.

    Salt-free is deliberate: a contract has to compare today's categories with
    the ones recorded last month, which only works if the hash is reproducible.
    """
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()
    return f"sha256:{digest[:length]}"


def mask_value(value: str) -> str:
    """Reduce a value to its shape (``ada@x.io`` → ``aaa@a.aa``)."""
    out: list[str] = []
    for char in str(value)[:64]:
        if char.isdigit():
            out.append("#")
        elif char.isalpha():
            out.append("a")
        elif char.isspace():
            out.append("_")
        else:
            out.append(char)
    return "".join(out)


def apply_to_enum_value(value: str, mode: PrivacyMode) -> str:
    """Transform one enum member according to the active mode."""
    if mode is PrivacyMode.STRICT:
        return hash_value(value)
    return str(value)


__all__ = [
    "PrivacyMode",
    "DEFAULT_MODE",
    "coerce_mode",
    "sample_limit",
    "collects_samples",
    "hash_value",
    "mask_value",
    "apply_to_enum_value",
]
