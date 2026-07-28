"""Properties that are easy to break silently, and expensive when broken.

Nothing here tests a feature. Each test guards a claim made elsewhere — in the
README, or in a design decision whose violation produces working-but-terrible
behaviour rather than a failure.

The scan-count tests are the reason this file exists. Reading the source once per
column instead of once per batch is invisible: identical results, correct
findings, every other test green — and a 200-column profile took 50 seconds
instead of 9. A performance bug with no symptom needs an assertion, because
nobody is going to notice it by using the tool.
"""

from __future__ import annotations

import os
import re

from zeyvor.contract.violations import ViolationType
from zeyvor.engines.base import Dialect, Relation
from zeyvor.profile.patterns import patterns_for
from zeyvor.profile.sql import enum_sql, sample_sql, scalar_stats_sql, shape_sql

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MARKER = "READ_THE_SOURCE"
RELATION = Relation(sql=MARKER, name="orders", source_uri="orders.csv")
DIALECT = Dialect()

BATCH = [(index, f"col_{index}") for index in range(20)]


# ── one scan per query, not one per column ────────────────────────────────────


def test_shape_pass_reads_the_source_once():
    assert shape_sql(DIALECT, RELATION, BATCH).count(MARKER) == 1


def test_enum_pass_reads_the_source_once():
    assert enum_sql(DIALECT, RELATION, BATCH).count(MARKER) == 1


def test_sample_pass_reads_the_source_once():
    assert sample_sql(DIALECT, RELATION, BATCH).count(MARKER) == 1


def test_scalar_pass_reads_the_source_once():
    sql, _ = scalar_stats_sql(
        DIALECT, RELATION, [name for _, name in BATCH], patterns=patterns_for()
    )
    assert sql.count(MARKER) == 1


def test_a_single_column_batch_skips_the_cte():
    """One branch reads the source once regardless, and the CTE would only make
    the SQL in an error message harder to read."""
    sql = shape_sql(DIALECT, RELATION, [(0, "col_0")])
    assert "WITH" not in sql
    assert sql.count(MARKER) == 1


def test_results_do_not_depend_on_the_cte():
    """The optimisation has to be invisible in the output, not merely faster.

    A batch of one takes the no-CTE path and a batch of twenty takes the CTE
    path, so profiling the same file both ways compares the two code paths
    rather than a profile against itself.
    """
    from helpers import fixture_path
    from zeyvor.profile import ProfileOptions, Profiler
    from zeyvor.sources import resolve_source

    def run(batch: int):
        resolved = resolve_source(fixture_path("messy.csv"))
        try:
            return Profiler(resolved.engine, ProfileOptions(column_batch_size=batch)).profile(
                resolved.relation
            )
        finally:
            resolved.close()

    without_cte, with_cte = run(1), run(20)

    assert [c.name for c in without_cte.columns] == [c.name for c in with_cte.columns]
    assert without_cte.row_count == with_cte.row_count
    for left, right in zip(without_cte.columns, with_cte.columns, strict=True):
        assert left.distinct_count == right.distinct_count, left.name
        assert left.null_count == right.null_count, left.name
        assert [(s.shape, s.count) for s in left.shapes] == [
            (s.shape, s.count) for s in right.shapes
        ], left.name
        assert left.inferred_type is right.inferred_type, left.name


# ── batch size is the memory lever, so it has to be reachable ─────────────────


def test_batch_size_is_documented_in_help():
    """It was hidden behind argparse.SUPPRESS, which made the only real
    mitigation for an out-of-memory error undiscoverable."""
    import argparse

    from zeyvor.cli.main import build_parser

    subcommands = build_parser()._subparsers._group_actions[0].choices  # type: ignore[union-attr]
    for action in subcommands["profile"]._actions:
        if action.dest == "batch_size":
            assert action.help and action.help is not argparse.SUPPRESS
            assert "memory" in action.help.lower()
            return
    raise AssertionError("profile has no --batch-size flag")


def test_an_out_of_memory_error_names_the_flag_that_helps():
    """DuckDB's own advice points at settings this package owns and the user
    cannot reach, so it is replaced rather than passed through."""
    from zeyvor.engines.duckdb_engine import _out_of_memory_advice

    advice = _out_of_memory_advice(
        "Out of Memory Error: failed to allocate 32 KiB\n"
        "Possible solutions:\n* Reducing the number of threads (SET threads=X)"
    )
    assert "--batch-size" in advice
    assert "SET threads" not in advice


# ── the README has to keep telling the truth ──────────────────────────────────

WORDS = {
    20: "Twenty",
    21: "Twenty-one",
    22: "Twenty-two",
    23: "Twenty-three",
    24: "Twenty-four",
    25: "Twenty-five",
    26: "Twenty-six",
    27: "Twenty-seven",
    28: "Twenty-eight",
    29: "Twenty-nine",
    30: "Thirty",
}


def test_the_readme_counts_violation_types_correctly():
    """This drifted twice — once when relationships added three types, and once
    when the correction guessed. A number in prose needs a test or it rots."""
    with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as handle:
        readme = handle.read()
    actual = len(list(ViolationType))
    stated = re.search(r"(Twenty|Thirty)(-\w+)? violation types", readme)

    assert stated, "README no longer states a violation-type count"
    assert stated.group(0) == f"{WORDS[actual]} violation types", (
        f"README says {stated.group(0)!r}, but there are {actual} types"
    )


def test_every_violation_type_has_a_default_severity():
    """A type with no entry would fall back to whatever the lookup does, which
    is how a new finding silently becomes unreportable."""
    from zeyvor.contract.violations import DEFAULT_SEVERITY

    missing = [t.value for t in ViolationType if t not in DEFAULT_SEVERITY]
    assert not missing, f"no default severity for: {missing}"
