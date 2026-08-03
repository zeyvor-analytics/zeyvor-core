"""Turning a profile into a contract.

The governing rule: **only assert what the profile supports.** A generated
contract must pass against the data it was generated from, or the tool fails a
user on their very first run and never gets a second. Every clause below is
emitted from measured evidence, and the ones that cannot be established
confidently are simply left out.

The language model's role is deliberately narrow. It writes ``means`` — the
plain-English purpose that makes the rest of the clause reviewable — and it may
*remove* an assertion it judges unsafe. It can never add one. That asymmetry is
what stops a model from inventing a rule that breaks someone's build.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

from ..profile.models import InferredType, Observation, TableProfile
from .models import (
    CONTRACT_SCHEMA_VERSION,
    ColumnContract,
    Contract,
    Defaults,
    TableContract,
)

# Observations worth recording as accepted state, so that pre-existing mess does
# not resurface as a warning on the next run.
RECORDABLE_ISSUES = (
    Observation.NULL_WORDS,
    Observation.MOJIBAKE,
    Observation.MIXED_BOOLEAN_ENCODING,
    Observation.MIXED_TYPES,
    Observation.WHITESPACE_PADDING,
    Observation.INCONSISTENT_CASE,
    Observation.LEADING_ZEROS,
    Observation.EPOCH_SUSPECTED,
    Observation.EXCEL_SERIAL_SUSPECTED,
)

TEMPORAL_TYPES = (InferredType.DATE, InferredType.TIMESTAMP)
NUMERIC_TYPES = (InferredType.INTEGER, InferredType.FLOAT)

# Findings that mean "these values are numbers, whatever their storage type".
NUMERIC_IN_DISGUISE = (
    Observation.NUMERIC_STORED_AS_TEXT,
    Observation.CURRENCY_IN_TEXT,
    Observation.PERCENT_IN_TEXT,
    Observation.THOUSANDS_SEPARATORS,
)

# Columns whose *name* says they hold personal data. Marking these `no_pii`
# would be nonsense, so the clause is withheld regardless of what was measured.
PII_NAME_HINTS = (
    "email",
    "mail",
    "phone",
    "mobile",
    "tel",
    "address",
    "street",
    "city",
    "zip",
    "postal",
    "ssn",
    "social",
    "passport",
    "licence",
    "license",
    "iban",
    "card",
    "account",
    "dob",
    "birth",
    "ip",
)

# `name` on its own was too broad a hint. Most columns ending in it name a thing
# rather than a person — coffee_name, product_name, file_name — and treating
# them as personal data withheld `no_pii` from a product label and, worse, left
# an eight-value menu open because names are not a closed vocabulary. Whose name
# it is decides that, so the qualifier is what gets matched.
PERSON_NAME_QUALIFIERS = (
    "full",
    "first",
    "last",
    "middle",
    "given",
    "sur",
    "maiden",
    "user",
    "customer",
    "client",
    "employee",
    "staff",
    "contact",
    "person",
    "patient",
    "student",
    "member",
    "author",
    "owner",
    "sender",
    "recipient",
    "applicant",
    "guest",
    "holder",
    "nick",
    "screen",
    "display",
    "login",
)


def _looks_like_person_name(lowered: str) -> bool:
    stem = lowered.rstrip("s")
    if stem == "name":
        return True
    if not stem.endswith("name"):
        return False
    qualifier = stem[: -len("name")].strip("_- ")
    return qualifier in PERSON_NAME_QUALIFIERS


@dataclass(frozen=True)
class RangePolicy:
    """How much headroom a generated range clause gets.

    The tension is real in both directions. A `max` set at the observed maximum
    fails on the next larger order — a false alarm on ordinary business growth.
    Padded too generously, a unit change (dollars becoming cents) slips through
    unnoticed, and that is one of the failures worth catching most.

    What resolves it is the size of the gap between the two. Legitimate growth
    tends to be a factor of two or three; a unit shift is a factor of a hundred
    or a thousand. So the default headroom is 2x rounded up to one significant
    figure, which absorbs growth while leaving an order-of-magnitude move far
    outside the envelope.
    """

    numeric_headroom: float = 2.0
    zero_floor_ratio: float = 0.1
    """How near zero an observed minimum has to sit before zero is used as the
    floor. Below a tenth of the maximum the column plainly operates near zero and
    reaching it is ordinary; well above, zero is outside the data's range and
    naming it as the bound asserts nothing."""

    floor_non_negative_at_zero: bool = True
    """Where nothing negative was observed, use zero as the lower bound rather
    than the observed minimum. A negative value in a quantity or price column is
    a genuine signal, and zero is a bound a reviewer immediately understands."""

    row_headroom: float = 2.0
    """How far a table is allowed to shrink before `min_rows` fails.

    The same reasoning as `numeric_headroom`, applied to volume. A table that
    halves has usually lost a partition, a join key, or a day of ingestion;
    ordinary week-to-week variation is nowhere near that. Deriving the floor
    from the observed count is what makes the clause an assertion at all — a
    fixed `min_rows: 1` passes on a file that arrived with one row, which is
    the most common shape of silent upstream failure."""

    null_rate_headroom: float = 0.02
    max_shapes_for_format_clause: int = 3
    min_shape_coverage: float = 0.99
    max_categories: int = 50

    max_unseen_category_mass: float = 0.01
    """How much of the vocabulary may still be unobserved before the set is left
    open, estimated by the share of values seen exactly once. The estimate scales
    with the sample the way confidence should: one singleton in forty rows is 2.5%
    still missing and far too shaky to close, while one in ten thousand is 0.01%
    and no reason to hold back."""

    max_numeric_categories: int = 12
    """Tighter than `max_categories`, because a number is only a label when there
    are very few of them. Real code sets are small — a Likert scale, a status
    enum, the months — while the distinct values of a genuine count keep
    arriving. Set to 0 to switch the behaviour off and treat every number as a
    measurement."""

    max_distinct_rate_for_categories: float = 0.15
    """A vocabulary is a small set of values used many times. Above this share of
    distinct values the column is a list of things seen, not a set of allowed
    ones — closing it would fail on the next new entry."""

    small_category_ceiling: int = 12
    """A very small set can still be a vocabulary in a small sample, provided
    each member appears several times."""


DEFAULT_RANGE_POLICY = RangePolicy()


def _round_up_1sf(value: float) -> float:
    """Round up to one significant figure: 733 → 800, 0.42 → 0.5."""
    if value == 0:
        return 0.0
    magnitude = math.floor(math.log10(abs(value)))
    step = 10**magnitude
    return math.ceil(value / step) * step


def _round_down_1sf(value: float) -> float:
    if value == 0:
        return 0.0
    magnitude = math.floor(math.log10(abs(value)))
    step = 10**magnitude
    return math.floor(value / step) * step


def _round_up_2sf(value: float) -> float:
    """Round up to two significant figures: 1128 → 1200, 154 → 160."""
    if value == 0:
        return 0.0
    magnitude = math.floor(math.log10(abs(value))) - 1
    step = 10**magnitude
    return math.ceil(round(value / step, 6)) * step


def _pad_upper(value: float, policy: RangePolicy) -> float:
    # Two significant figures, not one. Rounding the doubled value up to a single
    # figure quietly widened the headroom the policy documents: a cholesterol
    # maximum of 564 doubled to 1128 and then rounded to 2000, which is 3.5x the
    # observed value, not 2x. The bound stays a readable round number either way.
    padded = _round_up_2sf(value * policy.numeric_headroom)
    return int(padded) if float(padded).is_integer() else padded


def _pad_lower(value: float, policy: RangePolicy, maximum: float | None = None) -> float:
    if value < 0:
        padded = _round_down_1sf(value * policy.numeric_headroom)
        return int(padded) if float(padded).is_integer() else padded
    if not policy.floor_non_negative_at_zero:
        padded = _round_down_1sf(value / policy.numeric_headroom)
        return int(padded) if float(padded).is_integer() else padded
    # Zero is the right floor when the column plausibly reaches it, and only
    # then. A discount that bottoms out at 0 should keep 0; an age that starts
    # at 29 or a blood pressure that starts at 94 should not, because flooring
    # those at zero throws the observed minimum away and leaves a bound no
    # corruption can trip — a 0 blood pressure passing the check is precisely
    # the reading that should stop the build.
    if maximum is None or value <= abs(maximum) * policy.zero_floor_ratio:
        return 0
    padded = _round_down_1sf(value / policy.numeric_headroom)
    return int(padded) if float(padded).is_integer() else padded


def _floor_rows(row_count: int, policy: RangePolicy) -> int:
    """The row floor to promise, given what was observed.

    Rounded down to one significant figure like every other generated bound, so
    the number reads as the deliberate approximation it is: 4,340 rows becomes
    2,000, not 2,170.
    """
    return max(1, int(_round_down_1sf(row_count / policy.row_headroom)))


def _looks_like_pii_column(name: str) -> bool:
    lowered = name.lower()
    if _looks_like_person_name(lowered):
        return True
    return any(hint in lowered for hint in PII_NAME_HINTS)


KEY_NAME_SUFFIXES = ("id", "key", "code", "uuid", "guid", "no", "num", "number", "sku", "ref")


def _looks_like_key_column(name: str) -> bool:
    """Does the name suggest an identifier rather than a measurement?"""
    lowered = name.lower().strip()
    if lowered in KEY_NAME_SUFFIXES:
        return True
    return any(lowered.endswith("_" + suffix) for suffix in KEY_NAME_SUFFIXES)


# Words that describe a magnitude. A column named for one is open at the top
# however few values it holds today: `error_count` sitting at 0-2 all week is a
# count that reaches 3 eventually, not a three-word vocabulary.
QUANTITY_NAME_HINTS = (
    "count",
    "qty",
    "quantity",
    "total",
    "sum",
    "amount",
    "size",
    "bytes",
    "length",
    "duration",
    "age",
    "score",
    "price",
    "cost",
    "rank",
    "index",
    "offset",
    # Singular stems, which match their own plurals as substrings. `retries` is
    # the exception that needs spelling out.
    "view",
    "click",
    "error",
    "attempt",
    "failure",
    "retry",
    "retries",
)


def _looks_like_quantity_column(name: str) -> bool:
    lowered = name.lower()
    return any(hint in lowered for hint in QUANTITY_NAME_HINTS)


def _is_small_integer_code(column, policy: RangePolicy) -> bool:
    """Small integers that label rather than measure.

    Half the columns in a clinical or survey extract are codes: chest pain type
    0-3, an ECG result 0-2, a Likert answer 1-5. They are categories that happen
    to be written as digits, and treating them as numbers produced the worst
    clause this tool generates — `min: 0, max: 6` on a four-value code, which
    admits 4 and 5 without comment and can never fail on anything a reviewer
    would care about.

    Distinguishing a code from a count cannot be done from the values alone;
    `cp` holding 0-3 and `item_count` holding 0-3 are identical in the data. So
    the guards are cardinality far below what a count reaches, deep repetition,
    integers only, and a name that does not describe a magnitude.

    Where it still guesses wrong it guesses tight, which is the recoverable
    direction: a closed set is one visible line in a file the workflow already
    asks a human to read and correct, while a range nothing can violate is
    invisible and silently protects nothing.
    """
    if column.inferred_type is not InferredType.INTEGER:
        return False
    if column.enum is None or column.enum.cardinality > policy.max_numeric_categories:
        return False
    return not _looks_like_quantity_column(column.name)


def _should_close_categories(column, policy: RangePolicy) -> bool:
    """Is this column's value set a vocabulary, or just the values it happens to hold?

    Getting this wrong is expensive in both directions, and running against real
    data made the failure mode obvious: a `phone` column with twenty distinct
    values had its set closed, which simultaneously enshrined 'notanumber' as
    required vocabulary and guaranteed a failure on the next legitimate number.

    Two guards. Repetition has to be real — a vocabulary is a small set of values
    used many times, not a list of everything seen once or twice. And columns
    whose *name* describes open-world data (people, places, contact details) are
    never closed, however few values today's sample happens to contain.
    """
    enum = column.enum
    if enum is None or not enum.complete or enum.hashed:
        return False
    if not (1 < enum.cardinality <= policy.max_categories):
        return False
    if column.is_unique:
        return False
    # Has the sample actually exhausted the vocabulary, or just run out of rows?
    # Values seen exactly once are the evidence: where singletons exist, more
    # unseen values almost certainly do too, and their share of the sample
    # estimates how much probability mass is still missing. Forty survey
    # responses closed a 1-7 preference scale at the six ranks that happened to
    # appear, so the next respondent using the seventh broke a build over data
    # that was never wrong.
    observed = sum(member.count for member in enum.members)
    singletons = sum(1 for member in enum.members if member.count == 1)
    if observed and singletons / observed > policy.max_unseen_category_mass:
        return False
    # The distinct values of a count or a price are not a vocabulary: item_count
    # holding 0-6 today will hold 7 tomorrow. Small integer codes are the
    # exception, and a common one — see _is_small_integer_code.
    if column.inferred_type in TEMPORAL_TYPES:
        return False
    if column.inferred_type in NUMERIC_TYPES and not _is_small_integer_code(column, policy):
        return False
    if _looks_like_pii_column(column.name):
        return False
    # Numbers in disguise are still numbers. A price column stored as '$150.00'
    # infers as text, but its values are open-world exactly like a float's.
    if any(column.has(o) for o in NUMERIC_IN_DISGUISE):
        return False

    distinct_rate = column.distinct_rate or 1.0
    well_repeated = distinct_rate <= policy.max_distinct_rate_for_categories
    small_and_repeated = (
        enum.cardinality <= policy.small_category_ceiling
        and column.non_null_count >= enum.cardinality * 3
    )
    return well_repeated or small_and_repeated


def _should_pin_format(column, policy: RangePolicy) -> bool:
    """Is this column's shape a guarantee, or a coincidence of the sample?

    Two exclusions matter, and both were found by generating a contract and
    immediately checking it against its own source data:

    Numbers are excluded, because their shape is a digit count and digit counts
    grow — pinning ``####`` on an id column schedules a failure for the day it
    reaches 10,000.

    Zero-padded numbers are the exception, and excluding them too was a real
    miss. Padding is what fixes a width: ``00123`` is five characters because
    someone decided the code is five characters, not because the count happens
    to be small today. Dropping the exception meant `account_code` got
    ``type: integer`` and no format clause, so the load that turned ``00123``
    into ``123`` — the ordinary consequence of reading the column as a number —
    passed the check clean. `leading_zeros` was measured and recorded the whole
    time; nothing asserted it.

    Elsewhere the shapes must either be temporal (where several genuine formats
    coexist and each is meaningful) or all the same length. Email addresses vary
    in length forever, so their shapes are incidental; a fixed-width code's are
    structural.
    """
    if not column.shapes:
        return False
    padded = Observation.LEADING_ZEROS.value in column.observations
    if column.inferred_type in NUMERIC_TYPES and not padded:
        return False
    coverage = column.shape_coverage or 0.0
    distinct_shapes = column.shape_distinct_count or len(column.shapes)
    if coverage < policy.min_shape_coverage:
        return False
    if distinct_shapes > policy.max_shapes_for_format_clause:
        return False
    if column.inferred_type in TEMPORAL_TYPES:
        return True
    lengths = {len(shape.shape) for shape in column.shapes if shape.shape}
    return len(lengths) == 1


def _date_part(iso: str | None) -> str | None:
    if not iso:
        return None
    return iso.split("T")[0].split(" ")[0]


def _year_start(iso: str | None) -> str | None:
    date = _date_part(iso)
    if not date or len(date) < 4:
        return None
    return f"{date[:4]}-01-01"


def generate_column_contract(
    column,
    *,
    policy: RangePolicy = DEFAULT_RANGE_POLICY,
) -> ColumnContract:
    """Build a column clause from measured evidence alone."""
    contract = ColumnContract(name=column.name)

    if column.inferred_type is InferredType.EMPTY:
        # Nothing was observed, so nothing can be promised. Recording the column
        # without assertions still documents that it is expected to exist.
        contract.means = None
        return contract

    contract.type = column.inferred_type.value

    # ── presence ──────────────────────────────────────────────────────────────
    if column.null_count == 0:
        contract.nullable = False
    else:
        observed = column.null_rate or 0.0
        contract.nullable = True
        contract.max_null_rate = min(
            1.0, round(_round_up_1sf(observed + policy.null_rate_headroom), 4)
        )

    # Uniqueness is only asserted where the column plausibly *is* a key. Any
    # column can be accidentally unique in one sample — 100 distinct prices in
    # 100 rows proves nothing — and `unique: true` on such a column breaks the
    # build the first time two rows legitimately agree. Missing a real key costs
    # a detection; a spurious one costs the user's trust.
    if column.is_unique and (
        _looks_like_key_column(column.name) or column.inferred_type is InferredType.UUID
    ):
        contract.unique = True

    # ── format ────────────────────────────────────────────────────────────────
    if _should_pin_format(column, policy):
        contract.formats = [shape.shape for shape in column.shapes if shape.shape]

    # ── categories ────────────────────────────────────────────────────────────
    if _should_close_categories(column, policy):
        contract.categories = sorted(column.enum.values())
        contract.categories_closed = True

    # ── range ─────────────────────────────────────────────────────────────────
    # Identifiers get no range clause. An auto-incrementing id grows past every
    # ceiling eventually, so `max` on a key is a scheduled false alarm; the
    # useful assertion for a key is uniqueness, which is handled above.
    # A closed set is the stronger statement and already excludes everything a
    # range would. Emitting both adds a bound that can only ever be redundant.
    is_key = _looks_like_key_column(column.name)
    if is_key or contract.categories_closed:
        pass
    elif column.inferred_type in NUMERIC_TYPES and column.numeric:
        if column.numeric.minimum is not None:
            contract.minimum = _pad_lower(
                column.numeric.minimum, policy, maximum=column.numeric.maximum
            )
        if column.numeric.maximum is not None:
            contract.maximum = _pad_upper(column.numeric.maximum, policy)
        # A bound on a whole-number column has to be a whole number. Padding a
        # rank of 1 downward produced `min: 0.5` on a column typed `integer`,
        # which reads as a contradiction to anyone reviewing the file. Widening
        # in both directions keeps every value the padding meant to admit.
        if column.inferred_type is InferredType.INTEGER:
            if contract.minimum is not None:
                contract.minimum = math.floor(contract.minimum)
            if contract.maximum is not None:
                contract.maximum = math.ceil(contract.maximum)
    elif column.inferred_type in TEMPORAL_TYPES and column.temporal:
        contract.minimum = _year_start(column.temporal.minimum)
        # Historic data gets a moving upper bound, which is both more useful and
        # more stable than a fixed date that goes stale the day it is written.
        if column.temporal.future_count == 0:
            contract.maximum = "today"
        else:
            contract.maximum = _date_part(column.temporal.maximum)

    # ── privacy ───────────────────────────────────────────────────────────────
    if not column.pii_signals and not _looks_like_pii_column(column.name):
        contract.no_pii = True

    # ── accepted state ────────────────────────────────────────────────────────
    recordable = {o.value for o in RECORDABLE_ISSUES}
    contract.known_issues = [o for o in column.observations if o in recordable]

    return contract


def generate_table_contract(
    profile: TableProfile,
    *,
    policy: RangePolicy = DEFAULT_RANGE_POLICY,
) -> TableContract:
    table = TableContract(
        name=profile.name,
        source=profile.source_uri,
        profile_fingerprint=profile.fingerprint(),
        min_rows=_floor_rows(profile.row_count, policy) if profile.row_count > 0 else None,
        allow_new_columns=True,
    )
    for column in profile.columns:
        table.columns[column.name] = generate_column_contract(column, policy=policy)
    return table


def generate_contract(
    profiles: TableProfile | list[TableProfile],
    *,
    policy: RangePolicy = DEFAULT_RANGE_POLICY,
    describer=None,
) -> Contract:
    """Generate a contract from one or more profiles.

    Args:
        profiles: what was measured.
        policy: how much headroom range and format clauses get.
        describer: optional callable taking (TableProfile, TableContract) and
            returning ``{column_name: description}``. This is where the language
            model plugs in; without it the contract is still complete and valid,
            just undocumented. See ``llm.py``.
    """
    if isinstance(profiles, TableProfile):
        profiles = [profiles]

    from .. import __version__

    contract = Contract(
        version=CONTRACT_SCHEMA_VERSION,
        generated_by=f"zeyvor {__version__}",
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        defaults=Defaults(),
    )

    for profile in profiles:
        table = generate_table_contract(profile, policy=policy)
        if describer is not None:
            try:
                described = describer(profile, table) or {}
            except Exception as exc:  # noqa: BLE001 - description is a nicety
                # A failed description must never cost a usable contract.
                described = {}
                contract.generated_by += f"  # descriptions unavailable: {exc}"
            for column_name, description in described.items():
                column = table.columns.get(column_name)
                if column is not None and description:
                    column.means = str(description).strip()
        contract.tables[table.name] = table

    return contract


__all__ = [
    "RangePolicy",
    "DEFAULT_RANGE_POLICY",
    "generate_column_contract",
    "generate_table_contract",
    "generate_contract",
]
