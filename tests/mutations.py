"""Known breakages, injected on purpose, so recall can be measured rather than asserted.

Every threshold in `contract/generate.py` carries a comment justifying it —
two-times headroom here, a 15% distinct-rate ceiling there — and every one of
those numbers was chosen by reasoning about what ought to work. That is not the
same as knowing. The README claims this tool catches things conventional tests
miss; nothing in the repository measured whether it actually does.

This module is the measurement. Each entry takes data that is known-clean,
breaks it in one specific, realistic way, and names the finding that ought to
fire. The harness runs `init` against the clean copy and `check` against the
broken one — the real sequence a user lives through — and records what came
back.

Two numbers come out, and the second one matters as much as the first:

**Recall.** Did the expected finding appear? A zero here is a hole in the
product, not a broken test.

**Collateral.** How many *other* findings appeared alongside it. One upstream
change producing nine findings is how a tool teaches people to stop reading its
output, and cascade suppression exists specifically to keep that number near
zero. Nothing measured it until now.

Mutations operate on `(header, rows)` of strings, because that is what a CSV is
before anything has decided what the values mean — mutating a parsed frame
would quietly launder exactly the type confusion several of these exist to
model.
"""

from __future__ import annotations

import csv
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

Table = tuple[list[str], list[list[str]]]

# Fixed so a recall number is reproducible. A mutation that only fails on some
# seeds is a flaky test pretending to be a measurement.
SEED = 20260819


@dataclass(frozen=True)
class Mutation:
    """One realistic way data goes wrong."""

    name: str
    expects: tuple[str, ...]
    """Violation type values that *should* fire. Multiple means any one of them
    counts as a catch — `epoch_suspected` and `type_contaminated` describe the
    same upstream change at different levels of specificity, and insisting on
    both would penalise the cascade suppression that is working as designed."""

    apply: Callable[[Table], Table]
    models: str
    """The incident this stands in for, in one line. If a mutation cannot be
    described as something that plausibly happened to somebody, it is a
    synthetic test rather than evidence."""

    contract_patch: Callable[[str], str] | None = None
    """Rewrites the generated `zeyvor.yml` before `check` runs. Some clauses are
    never generated — `allow_new_columns: false` is opt-in, freshness windows and
    cross-column rules are written by hand — and without this the harness could
    only ever measure the subset of the product that `init` produces by itself."""

    prepare: Callable[[Table], Table] | None = None
    """Applied to the clean copy *before* `init`, when the fixture does not
    already contain the shape this mutation needs to break. Leading-zero loss
    only means anything if the codes were padded to begin with, and building
    that into the shared fixture would change every other mutation's baseline."""


def _column(header: list[str], name: str) -> int:
    return header.index(name)


# ── the mutations ─────────────────────────────────────────────────────────────


def _epoch_swap(table: Table) -> Table:
    """Dates start arriving as Unix timestamps. The flagship failure."""
    header, rows = table
    i = _column(header, "signup_date")
    for row in rows[:3]:
        row[i] = str(int(datetime.strptime(row[i], "%Y-%m-%d").timestamp()))
    return header, rows


def _cents_shift(table: Table) -> Table:
    """An upstream system switches currency units. Type is identical throughout."""
    header, rows = table
    i = _column(header, "amount")
    for row in rows:
        row[i] = f"{float(row[i]) * 100:.0f}"
    return header, rows


def _type_contamination(table: Table) -> Table:
    """A fraction of a numeric column arrives as text. Small enough to survive a
    spot check, large enough to break a SUM."""
    header, rows = table
    i = _column(header, "amount")
    rng = random.Random(SEED)
    for row in rng.sample(rows, max(1, len(rows) // 50)):
        row[i] = "n/a"
    return header, rows


def _new_category(table: Table) -> Table:
    """A status nobody downstream has a branch for."""
    header, rows = table
    i = _column(header, "status")
    rows[0][i] = "awaiting_pickup"
    return header, rows


def _category_vanishes(table: Table) -> Table:
    """A status stops appearing — often a filter applied one stage too early."""
    header, rows = table
    i = _column(header, "status")
    for row in rows:
        if row[i] == "delivered":
            row[i] = "shipped"
    return header, rows


def _null_words(table: Table) -> Table:
    """'N/A' as a string. Counts as present to every null check ever written."""
    header, rows = table
    i = _column(header, "country")
    for row in rows[:6]:
        row[i] = "N/A"
    return header, rows


def _mojibake(table: Table) -> Table:
    """An encoding step broke upstream; UTF-8 read as Latin-1."""
    header, rows = table
    i = _column(header, "customer_email")
    for row in rows[:5]:
        row[i] = row[i].replace("user", "usÃ©r")
    return header, rows


def _pad_codes(table: Table) -> Table:
    """Give the fixture zero-padded codes, so there is something to lose."""
    header, rows = table
    i = _column(header, "country")
    codes = ["00123", "00456", "00789"]
    for index, row in enumerate(rows):
        row[i] = codes[index % len(codes)]
    return header, rows


def _strip_padding(table: Table) -> Table:
    """A zero-padded code went through a spreadsheet and came back an integer.
    Joins against it now miss, silently and completely."""
    header, rows = table
    i = _column(header, "country")
    for row in rows:
        row[i] = row[i].lstrip("0") or "0"
    return header, rows


def _rows_collapse(table: Table) -> Table:
    """A load that half-worked. Every surviving row is perfectly valid."""
    header, rows = table
    return header, rows[: len(rows) // 10]


def _column_disappears(table: Table) -> Table:
    """A rename upstream that nobody announced."""
    header, rows = table
    i = _column(header, "country")
    return [h for n, h in enumerate(header) if n != i], [
        [v for n, v in enumerate(row) if n != i] for row in rows
    ]


def _column_appears(table: Table) -> Table:
    header, rows = table
    return header + ["promo_code"], [row + ["SPRING"] for row in rows]


def _nulls_appear(table: Table) -> Table:
    """A column that was never empty starts being empty."""
    header, rows = table
    i = _column(header, "customer_email")
    for row in rows[:20]:
        row[i] = ""
    return header, rows


def _uniqueness_lost(table: Table) -> Table:
    """A key gains duplicates. Nothing errors; every join through it fans out."""
    header, rows = table
    i = _column(header, "order_id")
    for row in rows[1:6]:
        row[i] = rows[0][i]
    return header, rows


def _range_exceeded(table: Table) -> Table:
    """A value outside the contracted ceiling, but not so far outside that it
    reads as a unit change. Past ten times the bound `unit_shift_suspected`
    fires and deliberately suppresses this one, so testing the range check at
    all means staying under that line."""
    header, rows = table
    i = _column(header, "amount")
    ceiling = max(float(r[i]) for r in rows)
    rows[0][i] = f"{ceiling * 3:.2f}"
    return header, rows


def _excel_serial(table: Table) -> Table:
    """Dates that went through a spreadsheet and came back as day counts."""
    header, rows = table
    i = _column(header, "signup_date")
    epoch = datetime(1899, 12, 30)
    for row in rows[:4]:
        row[i] = str((datetime.strptime(row[i], "%Y-%m-%d") - epoch).days)
    return header, rows


def _mixed_date_formats(table: Table) -> Table:
    """Two upstream systems, two conventions, one column."""
    header, rows = table
    i = _column(header, "signup_date")
    for row in rows[:10]:
        parsed = datetime.strptime(row[i], "%Y-%m-%d")
        row[i] = parsed.strftime("%d/%m/%Y")
    return header, rows


def _pii_appears(table: Table) -> Table:
    """Email addresses pasted into a field that never held them."""
    header, rows = table
    i = _column(header, "status")
    for row in rows[:4]:
        row[i] = "contact ops@example.com"
    return header, rows


def _add_clean_flag(table: Table) -> Table:
    """A flag column that is consistently true/false at init."""
    header, rows = table
    header = header + ["is_gift"]
    for index, row in enumerate(rows):
        row.append("true" if index % 2 else "false")
    return header, rows


def _boolean_encoding_mixed(table: Table) -> Table:
    """A second upstream system starts writing Y/N and 1/0 into the same flag."""
    header, rows = table
    i = _column(header, "is_gift")
    for index, row in enumerate(rows):
        row[i] = ["true", "false", "Y", "N", "1", "0"][index % 6]
    return header, rows


def _scale_into_thousands(table: Table) -> Table:
    """Give the fixture amounts large enough for a separator to appear at all.
    The first version of this mutation formatted values under 1,000 and changed
    nothing, which the harness scored as a missed detection rather than as the
    broken test it was."""
    header, rows = table
    i = _column(header, "amount")
    for row in rows:
        row[i] = f"{float(row[i]) * 1000:.2f}"
    return header, rows


def _numeric_as_text(table: Table) -> Table:
    """Thousands separators arrive in a numeric column; SUM() returns zero."""
    header, rows = table
    i = _column(header, "amount")
    for row in rows:
        row[i] = f"{float(row[i]):,.2f}"
    return header, rows


def _whitespace_padding(table: Table) -> Table:
    header, rows = table
    i = _column(header, "status")
    for row in rows[:8]:
        row[i] = f"  {row[i]}  "
    return header, rows


def _make_fresh(table: Table) -> Table:
    """Backdate nothing — set every row to today, so the table looks live at init."""
    header, rows = table
    i = _column(header, "signup_date")
    today = datetime.now().strftime("%Y-%m-%d")
    for row in rows:
        row[i] = today
    return header, rows


def _loader_stopped(table: Table) -> Table:
    """Nothing new has arrived for a month. Every row already written is still
    perfectly valid, which is exactly why nothing else notices."""
    header, rows = table
    i = _column(header, "signup_date")
    stale = datetime.now() - timedelta(days=30)
    for row in rows:
        row[i] = stale.strftime("%Y-%m-%d")
    return header, rows


def _break_cross_column_rule(table: Table) -> Table:
    """Two columns stop agreeing. Each is individually valid throughout."""
    header, rows = table
    i = _column(header, "item_count")
    for row in rows[:5]:
        row[i] = "99999"
    return header, rows


def _case_drift(table: Table) -> Table:
    """Half a category set arrives capitalised. GROUP BY now splits in two."""
    header, rows = table
    i = _column(header, "status")
    for row in rows[::2]:
        row[i] = row[i].upper()
    return header, rows


MUTATIONS: tuple[Mutation, ...] = (
    Mutation(
        "epoch_swap",
        ("epoch_suspected", "type_contaminated"),
        _epoch_swap,
        "An upstream API changed and dates began arriving as Unix timestamps.",
    ),
    Mutation(
        "dollars_to_cents",
        ("unit_shift_suspected", "range_exceeded"),
        _cents_shift,
        "A payments integration switched from dollars to cents. The type never changed.",
    ),
    Mutation(
        "type_contamination_2pct",
        ("type_contaminated",),
        _type_contamination,
        "A small share of a numeric column started arriving as text.",
    ),
    Mutation(
        "new_category",
        ("new_category",),
        _new_category,
        "A new status value appeared that no downstream branch handles.",
    ),
    Mutation(
        "category_disappeared",
        ("category_disappeared",),
        _category_vanishes,
        "A filter moved one stage too early and a whole status stopped appearing.",
    ),
    Mutation(
        "null_words",
        ("null_words_appeared",),
        _null_words,
        "'N/A' arrived as a literal string, counting as present to every null check.",
    ),
    Mutation(
        "mojibake",
        ("mojibake_appeared",),
        _mojibake,
        "An encoding step broke and UTF-8 was read as Latin-1.",
    ),
    Mutation(
        "row_count_collapse",
        ("row_count_below_min",),
        _rows_collapse,
        "A load half-worked. Every row that survived is valid.",
    ),
    Mutation(
        "column_removed",
        ("column_missing",),
        _column_disappears,
        "A column was renamed upstream without warning.",
    ),
    Mutation(
        "column_added",
        ("column_added",),
        _column_appears,
        "A new column appeared mid-pipeline, on a table under strict schema control.",
        # A new column is not a failure by default — `allow_new_columns` is true,
        # because most of the time it is news rather than breakage. This mutation
        # is about the teams who opt out of that.
        contract_patch=lambda y: y.replace(
            "    min_rows:", "    allow_new_columns: false\n    min_rows:", 1
        ),
    ),
    Mutation(
        "nulls_in_required_column",
        ("nullability_violated", "null_rate_exceeded"),
        _nulls_appear,
        "A column that was never empty started arriving empty.",
    ),
    Mutation(
        "uniqueness_lost",
        ("uniqueness_lost",),
        _uniqueness_lost,
        "A primary key gained duplicates; joins through it now fan out.",
    ),
    Mutation(
        "range_exceeded",
        ("range_exceeded",),
        _range_exceeded,
        "A value arrived orders of magnitude outside anything previously seen.",
    ),
    Mutation(
        "excel_serial",
        ("excel_serial_suspected", "type_contaminated"),
        _excel_serial,
        "Dates went through a spreadsheet and returned as serial day numbers.",
    ),
    Mutation(
        "mixed_date_formats",
        ("format_changed", "type_contaminated"),
        _mixed_date_formats,
        "Two upstream systems wrote two date conventions into one column.",
    ),
    Mutation(
        "pii_in_free_text",
        ("pii_appeared", "new_category"),
        _pii_appears,
        "Email addresses were pasted into a field that never held them.",
    ),
    Mutation(
        "numeric_as_text",
        ("type_changed", "type_contaminated", "format_changed"),
        _numeric_as_text,
        "Thousands separators appeared in a numeric column; SUM() returns zero.",
        prepare=_scale_into_thousands,
    ),
    Mutation(
        "whitespace_padding",
        ("new_category", "format_changed"),
        _whitespace_padding,
        "Values arrived padded with whitespace and stopped matching on join.",
    ),
    Mutation(
        "leading_zeros_lost",
        ("format_changed", "type_changed", "new_category"),
        _strip_padding,
        "A zero-padded code went through a spreadsheet; joins against it now miss.",
        prepare=_pad_codes,
    ),
    Mutation(
        "stale_data",
        ("stale_data",),
        _loader_stopped,
        "The loading job stopped a month ago; every row it already wrote stayed valid.",
        contract_patch=lambda y: y.replace(
            "      signup_date:\n", "      signup_date:\n        fresh_within: 24h\n", 1
        ),
        prepare=_make_fresh,
    ),
    Mutation(
        "cross_column_rule_broken",
        ("rule_violated",),
        _break_cross_column_rule,
        "Two columns stopped agreeing, while each stayed valid on its own.",
        contract_patch=lambda y: y.replace(
            "    columns:", "    rules:\n      - amount >= item_count\n    columns:", 1
        ),
    ),
    Mutation(
        "case_drift",
        ("new_category",),
        _case_drift,
        "Half a category set arrived capitalised, splitting every GROUP BY in two.",
    ),
    Mutation(
        "mixed_boolean_encoding",
        ("mixed_boolean_encoding",),
        _boolean_encoding_mixed,
        "One flag column began carrying true/false, Y/N and 1/0 at the same time.",
        prepare=_add_clean_flag,
    ),
)


def read_table(path: str) -> Table:
    with open(path, newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    return rows[0], [list(r) for r in rows[1:]]


def write_table(path: str, table: Table) -> None:
    header, rows = table
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


__all__ = ["MUTATIONS", "Mutation", "Table", "read_table", "write_table"]
