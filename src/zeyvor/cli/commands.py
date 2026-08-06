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
    dump,
    generate_contract,
    load,
)
from ..contract.generate import generate_column_contract
from ..contract.violations import ViolationType
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


def _expand_sources(sources: list[str], args, console: Console) -> list[str]:
    """Resolve any `#schema.*` wildcards into one source per real table.

    Applied to `init` and `check` alike: a contract generated from a wildcard
    has to be checkable by the same wildcard, or the second half of the workflow
    asks for the two hundred table names the first half just saved you from.
    """
    from ..sources import expand_tables

    out: list[str] = []
    for source in sources:
        try:
            expanded = expand_tables(
                source,
                threads=getattr(args, "threads", None),
                memory_limit=getattr(args, "memory_limit", None),
            )
        except (EngineError, ValueError) as exc:
            raise CliError(f"could not expand {source}: {exc}") from None
        if len(expanded) > 1:
            console.step(f"found {len(expanded)} tables matching {source.split('#', 1)[1]}")
        out.extend(expanded)
    return out


class TableGone(Exception):
    """The connection worked and the table was not there.

    Distinct from every other read failure on purpose. A refused connection or a
    bad password is a broken invocation — exit 2, wake the platform team. A table
    that was renamed or dropped is the data no longer matching what was promised,
    which is exit 1 and the data team, and is the difference between the two exit
    codes this tool documents.
    """


def _is_missing_table(message: str) -> bool:
    lowered = message.lower()
    return ("catalog error" in lowered or "does not exist" in lowered) and "table" in lowered


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
        if _is_missing_table(str(exc)):
            raise TableGone(str(exc)) from None
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


def _dbt_sources(args, console: Console) -> list[tuple[str, str]] | None:
    """(table, source) pairs from a dbt manifest, or None if --dbt was not used."""
    manifest_path = getattr(args, "dbt", None)
    if not manifest_path:
        return None
    from ..integrations.dbt import DbtError, load_manifest, manifest_version, sources_for

    try:
        manifest = load_manifest(manifest_path)
        pairs = sources_for(manifest, getattr(args, "warehouse", "") or "", select=args.models)
    except DbtError as exc:
        raise CliError(str(exc)) from None
    console.step(f"dbt manifest {manifest_version(manifest)} — {len(pairs)} model(s)")
    return pairs


def _profile_for_check(args, contract: Contract, console: Console) -> list[TableProfile]:
    dbt_pairs = _dbt_sources(args, console)
    if dbt_pairs is not None:
        profiles = []
        for table_name, source in dbt_pairs:
            if table_name not in contract.tables:
                continue  # a model with no contract yet is not a failure
            profile = _profile_one(source, args, console)
            profile.name = table_name
            profiles.append(profile)
        if not profiles:
            raise CliError(
                "none of the selected dbt models appear in the contract",
                "generate one first: zeyvor init --dbt target/manifest.json "
                '--warehouse "..." -o zeyvor/',
            )
        return profiles

    if args.sources:
        sources = _expand_sources(args.sources, args, console)
        profiles = []
        for source in sources:
            try:
                profiles.append(_profile_one(source, args, console, table=args.table))
            except TableGone:
                console.step(f"{source} is gone")
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
        try:
            profile = _profile_one(source, args, console)
        except TableGone:
            # Leave it out and let the comparison report `table_missing`, which
            # is what actually happened: the contract promised this table and it
            # is not there. Raising here instead would exit 2 — "you typed it
            # wrong" — for a table someone renamed or dropped upstream.
            console.step(f"{table_name} is gone from its source")
            continue
        profile.name = table_name
        profiles.append(profile)
    return profiles


# ── init ──────────────────────────────────────────────────────────────────────


def cmd_init(args, console: Console) -> int:
    # `init` asked for a specific table, so its absence is a mistyped command
    # rather than a data problem — the opposite of what it means during `check`.
    try:
        return _init(args, console)
    except TableGone as exc:
        raise CliError(str(exc)) from None


def _init(args, console: Console) -> int:
    output = args.output or DEFAULT_CONTRACT_PATH
    if os.path.exists(output) and not args.force:
        raise CliError(
            f"{output} already exists",
            "pass --force to overwrite it, or --output to write elsewhere",
        )

    dbt_pairs = _dbt_sources(args, console)
    if dbt_pairs is not None:
        profiles = []
        for table_name, source in dbt_pairs:
            profile = _profile_one(source, args, console)
            profile.name = table_name
            profiles.append(profile)
    else:
        if not args.sources:
            raise CliError(
                "nothing to profile",
                "pass a source, or a dbt manifest: --dbt target/manifest.json",
            )
        sources = _expand_sources(args.sources, args, console)
        profiles = [_profile_one(source, args, console, table=args.table) for source in sources]

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

    # Foreign keys, when there is more than one table to join. Rules only — no
    # model is asked, because a relationship is an assertion that fails builds and
    # the model is only ever allowed to remove assertions here, never invent them.
    if len(profiles) > 1:
        from ..relations import infer_relationships

        contract.relationships = infer_relationships(profiles)
        if contract.relationships:
            console.step(f"found {len(contract.relationships)} relationship(s)")

    # dump() decides between one file and a directory of per-table files, so
    # `-o zeyvor/` does the obvious thing for a project with many models.
    try:
        dump(contract, output)
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
    #
    # The path is shown absolute, not as the (often bare, often relative) string
    # the user typed. `zeyvor init` run from a home directory with no -o flag
    # would otherwise just say "Wrote zeyvor.yml" — true, but useless the moment
    # the reader does not already know which directory that was.
    location = os.path.abspath(output)
    if os.path.isdir(output):
        files = sorted(f for f in os.listdir(output) if f.endswith((".yml", ".yaml")))
        console.success(f"Wrote {len(files)} contract file(s) to {location}")
    else:
        console.success(f"Wrote {location}")
    console.out("")
    console.out(f"  {len(contract.tables)} table(s), {len(columns)} columns")
    console.out(f"  {closed} with a closed category set")
    console.out(f"  {formats} with a pinned format")
    console.out(f"  {ranges} with a range")
    if contract.relationships:
        console.out(f"  {len(contract.relationships)} relationship(s) between tables")
    if known:
        console.out(f"  {known} with pre-existing issues recorded as accepted")
    console.out("")
    if not described:
        console.out("  Descriptions were not written. Set ANTHROPIC_API_KEY and re-run")
        console.out("  with --force to add them, or fill in `means:` by hand.")
        console.out("")
    if contract.relationships:
        console.out("  Relationships were guessed from column names and uniqueness.")
        console.out("  Check them especially closely — a wrong one fails builds.")
        console.out("")
    console.out("  Read it, correct anything wrong, and commit it.")
    console.out("  Then: zeyvor check")


# ── check ─────────────────────────────────────────────────────────────────────


def _scope_to(contract: Contract, profiles: list[TableProfile]) -> Contract:
    """Narrow a contract to the tables actually being checked.

    Selecting explicitly — `--models orders`, or naming a source — is a request
    to check that thing, not an assertion that nothing else exists. Without this,
    `--models orders` reports the project's other forty models as missing.

    A bare `zeyvor check` keeps the full contract, so a table that genuinely
    cannot be profiled is still a failure.
    """
    names = {p.name for p in profiles}
    return Contract(
        version=contract.version,
        generated_by=contract.generated_by,
        generated_at=contract.generated_at,
        defaults=contract.defaults,
        tables={name: t for name, t in contract.tables.items() if name in names},
        # A relationship with one end out of scope cannot be measured, and
        # reporting it as unmeasurable on every scoped run would be noise.
        relationships=[
            relationship
            for relationship in contract.relationships
            if relationship.from_table in names and relationship.to_table in names
        ],
    )


def _table_sources(args, contract: Contract, profiles: list[TableProfile]) -> dict[str, str]:
    """Where each checked table was actually read from.

    Relationship measurement needs to re-open both sides in one engine, so it
    needs the source strings rather than the profiles. The profile records the
    URI it used, which is more reliable than re-deriving it: it already accounts
    for --table, for dbt, and for a single source checked against a single-table
    contract under a different name.
    """
    sources: dict[str, str] = {}
    for profile in profiles:
        if profile.source_uri:
            sources[profile.name] = profile.source_uri
    for name, table in contract.tables.items():
        sources.setdefault(name, table.source)
    return {name: source for name, source in sources.items() if source}


def _measure_relationships(
    args, contract: Contract, profiles: list[TableProfile], console: Console
):
    """Measure every relationship, one engine per pair.

    Both sides have to be readable from the same connection for a join to be
    possible at all. Opening the child's source gives an engine; the parent is
    then resolved *into that engine*, which works for two local files (DuckDB
    reads both) and for two tables in one warehouse (the usual case). A child and
    parent genuinely living in different systems cannot be joined here, and the
    attempt fails cleanly into a `relationship_uncheckable` warning rather than
    pretending the keys matched.
    """
    from ..relations import RelationshipMeasurement, measure_relationship
    from ..sources import resolve_source

    if not contract.relationships:
        return []

    sources = _table_sources(args, contract, profiles)
    measurements = []

    for relationship in contract.relationships:
        if relationship.ignore:
            continue

        child_source = sources.get(relationship.from_table)
        parent_source = sources.get(relationship.to_table)
        if not child_source or not parent_source:
            missing = relationship.from_table if not child_source else relationship.to_table
            measurements.append(
                RelationshipMeasurement(
                    relationship=relationship,
                    error=f"no source recorded for '{missing}'",
                )
            )
            continue

        console.step(f"checking {relationship.key}")
        resolved = None
        try:
            resolved = resolve_source(
                child_source,
                memory_limit=getattr(args, "memory_limit", None),
                threads=getattr(args, "threads", None),
            )
            parent = resolve_source(parent_source, engine=resolved.engine)
            measurements.append(
                measure_relationship(
                    resolved.engine, relationship, resolved.relation, parent.relation
                )
            )
        except (EngineError, FileNotFoundError, ValueError) as exc:
            measurements.append(
                RelationshipMeasurement(relationship=relationship, error=_one_line(exc))
            )
        finally:
            if resolved is not None:
                resolved.close()

    return measurements


def _one_line(exc: Exception, limit: int = 140) -> str:
    text = " ".join(str(exc).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def cmd_check(args, console: Console) -> int:
    contract = _load_contract(args)
    if args.warn_only:
        contract.defaults.on_violation = Severity.WARN

    profiles = _profile_for_check(args, contract, console)
    if args.sources or getattr(args, "models", None):
        contract = _scope_to(contract, profiles)
    report = check(profiles, contract)

    # Cross-table checks come after the per-column ones, and are skipped when a
    # table is already missing: a join cannot be measured against a table that is
    # not there, and reporting both would be two messages for one cause.
    if contract.relationships and not report.has(ViolationType.TABLE_MISSING):
        from ..relations import check_relationships

        measurements = _measure_relationships(args, contract, profiles, console)
        relationship_violations = check_relationships(
            contract, measurements, existing=report.violations
        )
        if relationship_violations:
            report.violations.extend(relationship_violations)
        report.relationships_checked = sum(1 for m in measurements if m.measured)

    payload = report.to_dict()
    fmt = "json" if getattr(args, "json", False) else getattr(args, "format", "text")

    if fmt == "json":
        console.out(json.dumps(payload, indent=2))
    elif fmt == "markdown":
        from ..integrations.publish import to_markdown

        console.out(to_markdown(payload, show_values=getattr(args, "show_values", False)))
    else:
        console.out(render_report(report, console))

    webhook = getattr(args, "slack_webhook", None)
    if webhook:
        from ..integrations.publish import post_to_slack, to_slack_blocks

        try:
            post_to_slack(
                webhook,
                to_slack_blocks(payload, show_values=getattr(args, "show_values", False)),
            )
            console.step("posted to Slack")
        except RuntimeError as exc:
            # A failed notification must not mask the check's own verdict.
            console.error(f"could not post to Slack: {exc}")

    if getattr(args, "upload", False):
        _upload_report(args, report, console)

    if args.fail_on_warn and report.warnings:
        return 1
    return report.exit_code


def _upload_report(args, report, console: Console) -> None:
    """Send the run to a Zeyvor account. Never changes the exit code.

    A reporting service being unreachable is not a data problem, so it must not
    turn a green build red — nor quietly swallow a real failure, which is why
    this runs after the verdict has already been printed.
    """
    from ..integrations.upload import (
        ACCOUNT_URL,
        PROJECT_ENV,
        UploadError,
        build_payload,
        post_report,
    )

    project = getattr(args, "project", None) or os.environ.get(PROJECT_ENV) or ""
    if not project:
        console.error(
            f"--upload needs a project, so this run was not reported. Reporting is "
            f"optional and the check itself was unaffected.\n"
            f"       Pass --project OWNER/NAME or set {PROJECT_ENV}. Projects are "
            f"created at {ACCOUNT_URL}."
        )
        return

    try:
        post_report(
            build_payload(report, project=project),
            endpoint=getattr(args, "endpoint", None),
        )
    except UploadError as exc:
        console.error(f"could not upload the report: {exc}")
        return
    console.step(f"reported to {project}")


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
        f"type            {column.inferred_type.value} ({column.type_confidence:.0%} confidence)",
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
        lines.append(f"temporal        {column.temporal.minimum} .. {column.temporal.maximum}")
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
        dump(contract, path)
        console.success(f"Updated {os.path.abspath(path)}")
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
