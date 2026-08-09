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


# ── things that only break on Windows ─────────────────────────────────────────
#
# Both of these passed on Linux and macOS and failed on the first Windows CI run
# of the published repo. They are pinned here because the failure mode is
# platform-specific, which means local development will never surface it.


def test_a_uri_path_loses_its_leading_slash_on_every_platform():
    """`duckdb:///wh.db` must open `wh.db`, on Windows too.

    Both the DuckDB and SQLite branches carried an `os.name != "nt"` guard that
    left the slash on Windows, so the engine was handed `//wh.db` and refused
    it. Fixing one and missing the other is why this test checks the behaviour
    rather than the text of a particular line.
    """
    from zeyvor.sources import _path_from_uri

    assert _path_from_uri("duckdb:///wh.db") == "wh.db"
    assert _path_from_uri("sqlite:///app.db") == "app.db"
    # A POSIX absolute path is written with four slashes and keeps one.
    assert _path_from_uri("duckdb:////var/data/wh.db") == "/var/data/wh.db"
    # A Windows absolute path needs the slash gone or it becomes //C:/...
    assert _path_from_uri("sqlite:///C:/data/app.db") == "C:/data/app.db"


def test_no_platform_branch_decides_how_a_path_is_read():
    """A path rule that differs per platform is how the Windows bug survived
    review twice. There is no legitimate one left in the package."""
    import ast
    import pathlib

    # Parsed, not grepped: the docstring that explains the old bug quotes the
    # very expression being banned, and a text search cannot tell prose from code.
    offenders = []
    for path in sorted(pathlib.Path(ROOT, "src").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            module = getattr(node.value, "id", None)
            if (module, node.attr) in {("os", "name"), ("sys", "platform")}:
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not offenders, "platform branch in: " + ", ".join(offenders)


TEXT_IO = ("open", "read_text", "write_text")


def test_every_file_is_opened_with_an_explicit_encoding():
    """Windows defaults to cp1252, so unqualified text IO round-trips through
    the wrong codec and blows up on the first em-dash.

    Covers `Path.read_text` and `Path.write_text` as well as `open`, because the
    first version of this test checked only `open` — and the second Windows CI
    run failed on exactly the calls it did not look at. The contract header's
    em-dash lands at byte 23, which is the position in every one of those
    tracebacks.

    Parsed rather than grepped: a line-based attempt flagged its own source and
    a docstring example, and missed a call split across two lines.
    """
    import ast
    import pathlib

    offenders = []
    for path in sorted(
        list(pathlib.Path(ROOT, "src").rglob("*.py"))
        + list(pathlib.Path(ROOT, "tests").rglob("*.py"))
    ):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name not in TEXT_IO:
                continue
            # A binary mode has no encoding to declare.
            modes = [
                a.value
                for a in node.args[1:2]
                if isinstance(a, ast.Constant) and isinstance(a.value, str)
            ]
            if name == "open" and any("b" in mode for mode in modes):
                continue
            if any(kw.arg == "encoding" for kw in node.keywords):
                continue
            offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")

    assert not offenders, "text IO without encoding=: " + ", ".join(offenders)


def test_no_subprocess_replaces_the_whole_environment():
    """`env={...}` wipes everything the OS put there.

    On Windows that removes SystemRoot, and the interpreter then cannot seed its
    hash randomisation — it dies with `_Py_HashRandomization_Init` before
    executing a line. That is what the third Windows CI run failed on, and no
    amount of adding variables to the literal would have been the fix: the
    environment has to be inherited and overlaid.
    """
    import ast
    import pathlib

    offenders = []
    for path in sorted(pathlib.Path(ROOT, "tests").rglob("*.py")) + sorted(
        pathlib.Path(ROOT, "src").rglob("*.py")
    ):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "attr", None) not in {"run", "Popen", "call", "check_output"}:
                continue
            for keyword in node.keywords:
                if keyword.arg != "env":
                    continue
                # A dict literal with no ** unpacking replaces rather than extends.
                if isinstance(keyword.value, ast.Dict) and not any(
                    key is None for key in keyword.value.keys
                ):
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")

    assert not offenders, (
        "subprocess env= replaces the environment instead of extending it "
        "({**os.environ, ...}): " + ", ".join(offenders)
    )


# ── the README is a promise the CLI has to keep ────────────────────────────────


def _readme_bash_blocks() -> list[str]:
    import pathlib
    import re

    readme = pathlib.Path(__file__).resolve().parent.parent / "README.md"
    return re.findall(r"```bash\n(.*?)```", readme.read_text(encoding="utf-8"), re.S)


def _cli_surface():
    """(subcommand names, every flag string the parser accepts anywhere)."""
    from zeyvor.cli.main import build_parser

    parser = build_parser()
    subcommands = parser._subparsers._group_actions[0].choices  # type: ignore[union-attr]
    flags = {opt for action in parser._actions for opt in action.option_strings}
    for sub in subcommands.values():
        for action in sub._actions:
            flags.update(action.option_strings)
    return set(subcommands), flags


def test_every_command_the_readme_shows_actually_exists():
    """Documentation drift is silent and expensive: the README is the PyPI page
    and the first thing a stranger copies from. Three separate errors shipped
    before this test existed — a flag documented comma-separated when it takes
    spaces, a Python version one minor behind the real floor, and a whole
    warehouse documented as working when it could not authenticate at all.
    """
    import re

    subcommands, _ = _cli_surface()
    seen = set()
    for block in _readme_bash_blocks():
        for line in block.splitlines():
            match = re.match(r"\s*zeyvor\s+([a-z][a-z-]*)", line)
            if match:
                seen.add(match.group(1))

    assert seen, "no zeyvor commands found in the README — has it moved?"
    unknown = seen - subcommands
    assert not unknown, f"README documents commands that do not exist: {sorted(unknown)}"


def test_every_flag_the_readme_shows_actually_exists():
    import re

    _, flags = _cli_surface()
    seen = set()
    for block in _readme_bash_blocks():
        for line in block.splitlines():
            if "zeyvor" not in line:
                continue
            seen.update(re.findall(r"(?<![\w-])(--[a-z][a-z-]+)", line))

    unknown = seen - flags
    assert not unknown, f"README documents flags that do not exist: {sorted(unknown)}"


def test_every_flag_the_action_passes_actually_exists():
    """The composite action is the only caller nobody can fix in place.

    A README typo costs a stranger one confused minute. A renamed flag in
    action.yml breaks the check in every repository pinned to @v1 at once, on
    their data, in their CI — and the error surfaces as exit code 2 in someone
    else's build log, which is the last place we would hear about it.
    """
    import pathlib
    import re

    action = pathlib.Path(__file__).resolve().parent.parent / "action.yml"
    _, flags = _cli_surface()

    seen = set()
    for line in action.read_text(encoding="utf-8").splitlines():
        # Only lines that build or issue a zeyvor command. The `gh api` calls
        # further down carry --jq and --paginate, and one of them mentions
        # zeyvor inside a jq filter, so matching on the word alone would drag
        # GitHub's CLI surface into this assertion.
        if not (
            'ARGS="$ARGS' in line
            or 'SHOW="--' in line
            or re.search(r"(?<![\w-])zeyvor\s+(?:[a-z][a-z-]*|--)", line)
        ):
            continue
        seen.update(re.findall(r"(?<![\w-])(--[a-z][a-z-]+)", line))

    assert seen, "no zeyvor flags found in action.yml — has it moved?"
    unknown = seen - flags
    assert not unknown, f"action.yml passes flags that do not exist: {sorted(unknown)}"


def test_the_readme_does_not_claim_an_old_version_of_itself():
    """The README is the PyPI landing page, so a stale version is the first
    thing a stranger reads.

    This one had actually rotted: the status line still said v0.1 at version
    0.7.0 — through a credential fix, two features and six releases. The same
    drift on the website was caught and fixed weeks earlier, and it survived
    here for exactly one reason, which is that nothing asserted it.

    Deliberately narrow. Only a `vX.Y.Z` written as a claim about this package
    counts; the action tag `@v1` and version numbers belonging to other things
    (`actions/checkout@v4`, `duckdb>=0.10.0`) are not claims about this one.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    version = re.search(
        r'^version\s*=\s*"([^"]+)"',
        (root / "pyproject.toml").read_text(encoding="utf-8"),
        re.M,
    )
    assert version, "pyproject has no version"
    current = version.group(1)

    readme = (root / "README.md").read_text(encoding="utf-8")
    claimed = set(re.findall(r"(?<![\w/@-])v(\d+\.\d+\.\d+)\b", readme))

    stale = sorted(claimed - {current})
    assert not stale, (
        f"README claims version(s) {stale} but this package is {current}. "
        "A version in prose is documentation, and documentation nobody owns goes stale."
    )


def test_any_stated_python_floor_matches_pyproject():
    """`pip install` on an unsupported Python fails with a resolver error that
    names no version, so a stated floor has to be the real one.

    Deliberately narrow: only phrasings that *state a requirement* count. An
    incidental mention — "Python 3.11+ ignores hidden .pth files" in a
    troubleshooting note — is a fact about CPython, not a claim about this
    package, and matching it would make this test cry wolf until someone
    deleted it.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    declared = re.search(
        r'requires-python\s*=\s*">=([\d.]+)"',
        (root / "pyproject.toml").read_text(encoding="utf-8"),
    )
    assert declared, "no requires-python in pyproject.toml"

    readme = (root / "README.md").read_text(encoding="utf-8")
    stated = re.findall(
        r"(?:requires |needs )?Python (\d+\.\d+)(?: or newer| or later|\+ required)",
        readme,
    )
    for version in stated:
        assert version == declared.group(1), (
            f"README states Python {version}, pyproject requires >={declared.group(1)}"
        )
