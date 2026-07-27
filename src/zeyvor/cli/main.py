"""Argument parsing and dispatch.

Exit codes are part of the interface, because CI reads them:

    0   everything matched
    1   the data violated the contract
    2   the invocation itself failed — bad path, unreadable contract, no key

Separating 1 from 2 matters. A build that goes red because a contract file has a
typo should be distinguishable from one that goes red because the data broke.
"""

from __future__ import annotations

import argparse
import sys

from .. import __version__
from ..integrations import upload
from .commands import (
    DEFAULT_CONTRACT_PATH,
    CliError,
    cmd_accept,
    cmd_check,
    cmd_explain,
    cmd_init,
    cmd_profile,
)
from .render import Console

EXIT_OK = 0
EXIT_VIOLATIONS = 1
EXIT_ERROR = 2

DESCRIPTION = """\
Semantic data quality: catch when your data stops meaning what your schema says.

  zeyvor init orders.csv      write a contract describing the data as it is now
  zeyvor check                verify live data against the committed contract
  zeyvor explain signup_date  show what a column promises, and what it does
  zeyvor accept               bless an intentional change
  zeyvor profile orders.csv   just look at the data
"""


def _global_flags_parser() -> argparse.ArgumentParser:
    """The global flags, repeated on every subcommand.

    Argparse only accepts options defined on the top-level parser *before* the
    subcommand, so `zeyvor check --quiet` would otherwise be an error — which is
    exactly how people type it. Declaring them again on each subcommand with
    SUPPRESS as the default makes both positions work: when the flag is absent
    here, the attribute is simply not written, so a value given before the
    subcommand survives instead of being clobbered by a second default.
    """
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument(
        "--no-colour",
        "--no-color",
        dest="no_colour",
        action="store_true",
        default=argparse.SUPPRESS,
        help=argparse.SUPPRESS,
    )
    parent.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Suppress progress narration",
    )
    parent.add_argument(
        "--debug", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS
    )
    return parent


def _add_dbt_options(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("dbt")
    group.add_argument(
        "--dbt",
        nargs="?",
        const="target/manifest.json",
        metavar="MANIFEST",
        help="Take tables from a dbt manifest (default: target/manifest.json)",
    )
    group.add_argument(
        "--warehouse",
        help='Connection the dbt models live in, e.g. "snowflake://ACCOUNT"',
    )
    group.add_argument(
        "--models",
        nargs="+",
        metavar="NAME",
        help="Limit to these dbt models",
    )


def _add_source_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--table", help="Table name, for database sources")
    parser.add_argument(
        "--privacy",
        default="masked",
        choices=["strict", "masked", "full"],
        help="How much value-level detail may leave the machine (default: masked)",
    )
    parser.add_argument("--memory-limit", dest="memory_limit", help="Cap engine memory, e.g. 1GB")
    parser.add_argument("--threads", type=int, help="Cap engine threads")
    parser.add_argument(
        "--batch-size", dest="batch_size", type=int, default=20, help=argparse.SUPPRESS
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zeyvor",
        description=DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"zeyvor {__version__}")
    parser.add_argument("--no-colour", "--no-color", dest="no_colour", action="store_true")
    parser.add_argument("-q", "--quiet", action="store_true", help="Suppress progress narration")
    parser.add_argument(
        "--debug", action="store_true", help="Show tracebacks instead of clean errors"
    )

    common = _global_flags_parser()
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    # ── init ──────────────────────────────────────────────────────────────────
    init = subparsers.add_parser(
        "init",
        parents=[common],
        help="Write a contract describing the data as it is now",
        description=(
            "Profiles one or more sources and writes a contract you should read, "
            "correct and commit. Descriptions are written by a language model when "
            "ANTHROPIC_API_KEY is available; nothing else ever needs a key."
        ),
    )
    init.add_argument(
        "sources", nargs="*", help="Files, globs, URLs or database URIs (or use --dbt)"
    )
    init.add_argument(
        "-o", "--output", default=DEFAULT_CONTRACT_PATH, help=f"Default: {DEFAULT_CONTRACT_PATH}"
    )
    init.add_argument("-f", "--force", action="store_true", help="Overwrite an existing contract")
    ai_group = init.add_mutually_exclusive_group()
    ai_group.add_argument(
        "--ai",
        dest="ai_flag",
        action="store_true",
        help="Require column descriptions (fails without a key)",
    )
    ai_group.add_argument(
        "--no-ai", dest="no_ai", action="store_true", help="Never contact a model"
    )
    _add_dbt_options(init)
    _add_source_options(init)
    init.set_defaults(func=cmd_init)

    # ── check ─────────────────────────────────────────────────────────────────
    check = subparsers.add_parser(
        "check",
        parents=[common],
        help="Verify live data against the committed contract",
        description=(
            "Compares data against the contract. Deterministic and offline: no "
            "model, no network, no API key. With no sources given, the ones "
            "recorded in the contract are used, which is what makes a bare "
            "`zeyvor check` work in CI."
        ),
    )
    check.add_argument("sources", nargs="*", help="Defaults to the contract's own sources")
    check.add_argument(
        "-c", "--contract", default=DEFAULT_CONTRACT_PATH, help=f"Default: {DEFAULT_CONTRACT_PATH}"
    )
    check.add_argument(
        "--format",
        choices=["text", "json", "markdown"],
        default="text",
        help="text for a terminal, json for a pipe, markdown for a PR comment",
    )
    check.add_argument("--json", action="store_true", help="Shorthand for --format json")
    check.add_argument(
        "--show-values",
        action="store_true",
        help="Include category values in published output (off by default: a PR "
        "comment is a publication, a terminal is not)",
    )
    check.add_argument("--slack-webhook", metavar="URL", help="Post the result to Slack")
    check.add_argument(
        "--warn-only",
        action="store_true",
        help="Report everything but always exit 0 (for a first adoption run)",
    )
    check.add_argument("--fail-on-warn", action="store_true", help="Treat warnings as failures too")
    check.add_argument(
        "--upload",
        action="store_true",
        help="Also send the run to your Zeyvor account, so the history survives "
        "the terminal. Off by default; no value from your data is ever sent",
    )
    check.add_argument(
        "--project",
        metavar="OWNER/NAME",
        help=f"Project to report to (or set {upload.PROJECT_ENV})",
    )
    check.add_argument(
        "--endpoint",
        metavar="URL",
        help=f"Override the reporting endpoint (or set {upload.ENDPOINT_ENV})",
    )
    _add_dbt_options(check)
    _add_source_options(check)
    check.set_defaults(func=cmd_check)

    # ── explain ───────────────────────────────────────────────────────────────
    explain = subparsers.add_parser(
        "explain",
        parents=[common],
        help="Show what a column promises, and what it currently does",
    )
    explain.add_argument("target", help="column, or table.column")
    explain.add_argument(
        "-c", "--contract", default=DEFAULT_CONTRACT_PATH, help=f"Default: {DEFAULT_CONTRACT_PATH}"
    )
    explain.add_argument("--source", help="Defaults to the source recorded in the contract")
    _add_source_options(explain)
    explain.set_defaults(func=cmd_explain)

    # ── accept ────────────────────────────────────────────────────────────────
    accept = subparsers.add_parser(
        "accept",
        parents=[common],
        help="Bless an intentional change by regenerating the affected clauses",
        description=(
            "Rewrites the clauses of the columns you name, or of the columns that "
            "currently fail. Prose and severity overrides are preserved, and every "
            "change is printed — this relaxes your checks, so it should be "
            "reviewed like any other change."
        ),
    )
    accept.add_argument("sources", nargs="*", help="Defaults to the contract's own sources")
    accept.add_argument(
        "-c", "--contract", default=DEFAULT_CONTRACT_PATH, help=f"Default: {DEFAULT_CONTRACT_PATH}"
    )
    accept.add_argument(
        "--column",
        dest="columns",
        action="append",
        help="Accept only this column (repeatable); defaults to every failing column",
    )
    accept.add_argument(
        "-n", "--dry-run", action="store_true", help="Show what would change, write nothing"
    )
    _add_dbt_options(accept)
    _add_source_options(accept)
    accept.set_defaults(func=cmd_accept)

    # ── profile ───────────────────────────────────────────────────────────────
    profile = subparsers.add_parser(
        "profile",
        parents=[common],
        help="Measure a source and show what is in it",
    )
    profile.add_argument("source", help="File, glob, URL or database URI")
    profile.add_argument("--json", action="store_true", help="Emit the raw profile JSON")
    _add_source_options(profile)
    profile.set_defaults(func=cmd_profile)

    return parser


def _normalise(args: argparse.Namespace) -> None:
    """Fold the mutually exclusive AI flags into a decision plus its provenance."""
    if hasattr(args, "no_ai"):
        args.ai_explicit = bool(getattr(args, "ai_flag", False))
        args.ai = args.ai_explicit or not args.no_ai
    if not hasattr(args, "sources") and hasattr(args, "source"):
        args.sources = [args.source]


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_OK

    _normalise(args)
    console = Console(colour=False if args.no_colour else None, quiet=args.quiet)

    try:
        return args.func(args, console)
    except CliError as exc:
        console.error(str(exc))
        if exc.hint:
            console.hint(exc.hint)
        return EXIT_ERROR
    except KeyboardInterrupt:  # pragma: no cover
        console.error("interrupted")
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001
        if args.debug:
            raise
        # A stack trace is never the right first thing to show a user.
        console.error(f"{type(exc).__name__}: {exc}")
        console.hint("re-run with --debug for the full traceback")
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
