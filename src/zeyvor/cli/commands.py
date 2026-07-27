"""The commands themselves.

Kept thin on purpose: Parts 1 and 2 did the work, so each command here is
argument handling, a call into the library, and output. Anything resembling
logic belongs behind the library boundary, where it can be tested without a
terminal.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from typing import Any

from ..contract import (
    Contract,
    ContractError,
    Severity,
    check,
    dumps,
    generate_contract,
    load,
)
from ..contract.generate import generate_column_contract
from ..engines.base import EngineError
from ..profile import ProfileOptions, TableProfile, profile_source
from .render import Console, render_report

DEFAULT_CONTRACT_PATH = "zeyvor.yml"


class CliError(Exception):
    """A problem with the invocation. Carries a hint where one helps."""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


# ── shared plumbing ───────────────────────────────────────────────────────────


def _profile_options(args) -> ProfileOptions:
    return ProfileOptions(
        privacy=getattr(args, "privacy", "masked"),
        column_batch_size=getattr(args, "batch_size", 20),
    )


def _profile_one(source: str, args, console: Console, *, table: str | None = None) -> TableProfile:
    console.step(f"profiling {source}")
    try:
        return profile_source(
            source,
            table=table,
            options=_profile_options(args),
            memory_limit=getattr(args, "memory_limit", None),
            threads=getattr(args, "threads", None),
        )
    except FileNotFoundError as exc:
        raise CliError(str(exc)) from None
    except (EngineError, ValueError) as exc:
        raise CliError(f"could not read {source}: {exc}") from None


def _load_contract(args) -> Contract:
    path = getattr(args, "contract", None) or DEFAULT_CONTRACT_PATH
    try:
        return load(path)
    except ContractError as exc:
        raise CliError(str(exc)) from None


def _sources_from_contract(contract: Contract) -> list[tuple[str, str]]:
    """(table name, source) for every table that recorded where it came from.

    This is what makes a bare `zeyvor check` work in CI: the contract already
    knows what it describes, so the command needs no arguments.
    """
    return [(name, table.source) for name, table in contract.tables.items() if table.source]


def _profile_for_check(args, contract: Contract, console: Console) -> list[TableProfile]:
    if args.sources:
        profiles = [_profile_one(source, args, console, table=args.table) for source in args.sources]
        # A single source checked against a single-table contract is the common
        # case, and insisting the names match would be pedantic.
        if len(profiles) == 1 and len(contract.tables) == 1:
            only = next(iter(contract.tables))
            if profiles[0].name != only:
                profiles[0].name = only
        return profiles

    recorded = _sources_from_contract(contract)
    if not recorded:
        raise CliError(
            "no sources given and the contract does not record any",
            "pass a source explicitly: zeyvor check orders.csv",
        )
    profiles = []
    for table_name, source in recorded:
        profile = _profile_one(source, args, console)
        profile.name = table_name
        profiles.append(profile)
    return profiles


# ── init ──────────────────────────────────────────────────────────────────────


def cmd_init(args, console: Console) -> int:
    output = args.output or DEFAULT_CONTRACT_PATH
    if os.path.exists(output) and not args.force:
        raise CliError(
            f"{output} already exists",
            "pass --force to overwrite it, or --output to write elsewhere",
        )

    profiles = [_profile_one(source, args, console, table=args.table) for source in args.sources]

    describer = None
    if args.ai:
        from ..contract.llm import ClaudeDescriber

        if os.environ.get("ANTHROPIC_API_KEY"):
            console.step("writing column descriptions")
            describer = ClaudeDescriber()
        elif args.ai_explicit:
            raise CliError(
                "--ai was requested but ANTHROPIC_API_KEY is not set",
                "export the key, or use --no-ai to generate without descriptions",
            )

    contract = generate_contract(profiles, describer=describer)

    try:
        with open(output, "w", encoding="utf-8") as handle:
            handle.write(dumps(contract))
    except OSError as exc:
        raise CliError(f"could not write {output}: {exc}") from None

    _report_what_was_written(contract, output, console, described=describer is not None)
    return 0


def _report_what_was_written(
    contract: Contract, output: str, console: Console, *, described: bool
) -> None:
    columns = [c for table in contract.tables.values() for c in table.columns.values()]
    closed = sum(1 for c in columns if c.categories_closed)
    formats = sum(1 for c in columns if c.formats)
    ranges = sum(1 for c in columns if c.minimum is not None or c.maximum is not None)
    known = sum(1 for c in columns if c.known_issues)

    # All of this is the command's result rather than narration, so it goes to a
    # single stream. Splitting it across stdout and stderr let the two buffers
    # interleave, and the success line surfaced after the summary it introduces.
    console.success(f"Wrote {output}")
    console.out("")
    console.out(f"  {len(contract.tables)} table(s), {len(columns)} columns")
    console.out(f"  {closed} with a closed category set")
    console.out(f"  {formats} with a pinned format")
    console.out(f"  {ranges} with a range")
    if known:
        console.out(f"  {known} with pre-existing issues recorded as accepted")
    console.out("")
    if not described:
        console.out("  Descriptions were not written. Set ANTHROPIC_API_KEY and re-run")
        console.out("  with --force to add them, or fill in `means:` by hand.")
        console.out("")
    console.out("  Read it, correct anything wrong, and commit it.")
    console.out("  Then: zeyvor check")


# ── check ─────────────────────────────────────────────────────────────────────


def cmd_check(args, console: Console) -> int:
    contract = _load_contract(args)
    if args.warn_only:
        contract.defaults.on_violation = Severity.WARN

    profiles = _profile_for_check(args, contract, console)
    report = check(profiles, contract)

    if args.json:
        console.out(json.dumps(report.to_dict(), indent=2))
    else:
        console.out(render_report(report, console))

    if args.fail_on_warn and report.warnings:
        return 1
    return report.exit_code


# ── explain ───────────────────────────────────────────────────────────────────


def cmd_explain(args, console: Console) -> int:
    """Show what the contract promises for one column, and what the data does."""
    contract = _load_contract(args)
    table_name, column_name = _split_target(args.target, contract)

    table = contract.tables.get(table_name)
    if table is None:
        raise CliError(
            f"the contract has no table '{table_name}'",
            f"it describes: {', '.join(contract.tables)}",
        )
    column = table.column(column_name)
    if column is None:
        raise CliError(
            f"the contract has no column '{column_name}' in {table_name}",
            f"columns: {', '.join(list(table.columns)[:12])}",
        )

    console.out(console.tint(f"{table_name}.{column_name}", "bold"))
    if column.means:
        console.out(console.wrap(column.means, indent="  "))
    console.out("")
    console.out("  Contract")
    for line in _clause_lines(column):
        console.out(f"    {line}")

    source = args.source or table.source
    if not source:
        console.out("")
        console.info("  Pass a source to compare the contract against live data.")
        return 0

    profile = _profile_one(source, args, console)
    profile.name = table_name
    measured = profile.get(column_name)
    console.out("")
    console.out("  Measured now")
    if measured is None:
        console.out("    the column is absent")
    else:
        for line in _measurement_lines(measured):
            console.out(f"    {line}")

        report = check(profile, contract)
        relevant = [v for v in report.violations if v.column == column_name]
        console.out("")
        if relevant:
            console.out(render_report(replace_violations(report, relevant), console))
        else:
            console.success("  This column matches its contract.")
    return 0


def replace_violations(report, violations):
    """A copy of a report narrowed to one column's findings."""
    return replace(report, violations=violations)


def _split_target(target: str, contract: Contract) -> tuple[str, str]:
    if "." in target:
        table, _, column = target.rpartition(".")
        return table, column
    if len(contract.tables) == 1:
        return next(iter(contract.tables)), target
    raise CliError(
        f"'{target}' is ambiguous — the contract describes several tables",
        "qualify it as table.column",
    )


def _clause_lines(column) -> list[str]:
    lines: list[str] = []
    if column.ignore:
        return ["ignored — this column is deliberately unchecked"]
    if column.type:
        lines.append(f"type            {column.type}")
    if column.formats:
        lines.append(f"formats         {', '.join(column.formats)}")
    if column.nullable is not None:
        lines.append(f"nullable        {str(column.nullable).lower()}")
    if column.max_null_rate is not None:
        lines.append(f"max_null_rate   {column.max_null_rate}")
    if column.unique is not None:
        lines.append(f"unique          {str(column.unique).lower()}")
    if column.categories is not None:
        closed = " (closed)" if column.categories_closed else " (open)"
        lines.append(f"categories      {', '.join(column.categories)}{closed}")
    if column.minimum is not None or column.maximum is not None:
        lines.append(f"range           {column.minimum} .. {column.maximum}")
    if column.no_pii:
        lines.append("no_pii          true")
    if column.known_issues:
        lines.append(f"known_issues    {', '.join(column.known_issues)}")
    if column.on_violation:
        lines.append(f"on_violation    {column.on_violation.value}")
    return lines or ["no clauses — nothing is checked for this column"]


def _measurement_lines(column) -> list[str]:
    lines = [
        f"type            {column.inferred_type.value} "
        f"({column.type_confidence:.0%} confidence)",
        f"rows            {column.row_count:,} "
        f"({column.null_count:,} null, {column.distinct_count:,} distinct)",
    ]
    if column.type_mixture and len(column.type_mixture) > 1:
        mixture = "  ".join(f"{k}={v:.1%}" for k, v in column.type_mixture.items())
        lines.append(f"mixture         {mixture}")
    if column.shapes:
        shapes = ", ".join(f"{s.shape} ({s.count:,})" for s in column.shapes[:4] if s.shape)
        lines.append(f"shapes          {shapes}")
    if column.enum and column.enum.complete:
        lines.append(f"values          {', '.join(column.enum.values()[:10])}")
    if column.numeric:
        lines.append(
            f"numeric         min={column.numeric.minimum} "
            f"median={column.numeric.p50} max={column.numeric.maximum}"
        )
    if column.temporal:
        lines.append(
            f"temporal        {column.temporal.minimum} .. {column.temporal.maximum}"
        )
    if column.pii_signals:
        lines.append(f"pii             {', '.join(column.pii_signals)}")
    if column.observations:
        lines.append(f"findings        {', '.join(column.observations)}")
    return lines


# ── accept ────────────────────────────────────────────────────────────────────


def cmd_accept(args, console: Console) -> int:
    """Bless an intentional change by regenerating the affected clauses.

    Deliberately narrow: it rewrites the clauses of columns you name, or of
    columns that currently fail, and it never touches the prose or the severity
    overrides a human wrote. Everything it changes is printed, because a command
    that silently relaxes your checks would be worse than editing YAML by hand.
    """
    path = getattr(args, "contract", None) or DEFAULT_CONTRACT_PATH
    contract = _load_contract(args)
    profiles = _profile_for_check(args, contract, console)
    by_name = {p.name: p for p in profiles}

    targets: dict[str, set[str]] = {}
    if args.columns:
        for target in args.columns:
            table_name, column_name = _split_target(target, contract)
            targets.setdefault(table_name, set()).add(column_name)
    else:
        # Only failures are accepted wholesale. A warning is something you chose
        # to leave as a warning, so silently rewriting its clause — quietly
        # blessing mojibake, say — would be the wrong default.
        report = check(profiles, contract)
        for violation in report.failures:
            if violation.column:
                targets.setdefault(violation.table, set()).add(violation.column)
        if not targets:
            console.success("Nothing to accept — no failures.")
            if report.warnings:
                warned = sorted({v.column for v in report.warnings if v.column})
                console.info("")
                console.info(
                    f"  {len(report.warnings)} warning(s) were not accepted. To bless one:"
                )
                console.info(f"    zeyvor accept --column {warned[0] if warned else 'NAME'}")
            return 0

    changes: list[str] = []
    for table_name, column_names in targets.items():
        table = contract.tables.get(table_name)
        profile = by_name.get(table_name)
        if table is None or profile is None:
            continue
        for column_name in sorted(column_names):
            measured = profile.get(column_name)
            existing = table.column(column_name)
            if measured is None or existing is None:
                continue
            regenerated = generate_column_contract(measured)
            # Preserve everything a person wrote by hand.
            regenerated.means = existing.means
            regenerated.ignore = existing.ignore
            regenerated.on_violation = existing.on_violation
            regenerated.known_issues = sorted(
                set(existing.known_issues) | set(regenerated.known_issues)
            )
            for line in _describe_clause_changes(existing, regenerated):
                changes.append(f"  {table_name}.{column_name}: {line}")
            table.columns[column_name] = regenerated

    if not changes:
        console.success("Nothing to accept — the contract already matches.")
        return 0

    if args.dry_run:
        console.out("Would change:")
    else:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(dumps(contract))
        console.success(f"Updated {path}")
    for line in changes:
        console.out(line)
    if not args.dry_run:
        console.info("")
        console.info("  Review the diff before committing — this relaxed your checks.")
    return 0


def _describe_clause_changes(before, after) -> list[str]:
    lines: list[str] = []
    for field, label in (
        ("type", "type"),
        ("formats", "formats"),
        ("nullable", "nullable"),
        ("max_null_rate", "max_null_rate"),
        ("unique", "unique"),
        ("categories", "categories"),
        ("categories_closed", "categories_closed"),
        ("minimum", "min"),
        ("maximum", "max"),
        ("no_pii", "no_pii"),
        ("known_issues", "known_issues"),
    ):
        old, new = getattr(before, field), getattr(after, field)
        if old != new:
            lines.append(f"{label}: {_short(old)} -> {_short(new)}")
    return lines


def _short(value: Any, limit: int = 60) -> str:
    text = "none" if value is None else str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ── profile ───────────────────────────────────────────────────────────────────


def cmd_profile(args, console: Console) -> int:
    """Measure a source and show what is in it. No contract involved."""
    profile = _profile_one(args.source, args, console, table=args.table)
    if args.json:
        console.out(profile.to_json())
        return 0

    console.out(
        console.tint(
            f"{profile.name} — {profile.row_count:,} rows × {profile.column_count} columns",
            "bold",
        )
    )
    console.out(
        console.tint(
            f"{profile.query_count} queries in {profile.duration_ms}ms · "
            f"privacy={profile.privacy_mode} · {profile.fingerprint()}",
            "dim",
        )
    )
    console.out("")
    for column in profile.columns:
        null_share = (column.null_count / column.row_count) if column.row_count else 0
        console.out(
            f"  {column.name:<28} {column.inferred_type.value:<10} "
            f"nulls={null_share:>4.0%}  distinct={column.distinct_count:,}"
        )
        if column.observations:
            console.out(console.tint(f"      {', '.join(column.observations)}", "yellow"))
    if profile.warnings:
        console.out("")
        for warning in profile.warnings:
            console.warning(warning)
    return 0


__all__ = [
    "CliError",
    "DEFAULT_CONTRACT_PATH",
    "cmd_accept",
    "cmd_check",
    "cmd_explain",
    "cmd_init",
    "cmd_profile",
]
