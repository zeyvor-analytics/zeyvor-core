"""Reading and writing ``zeyvor.yml``.

Two things this module takes seriously.

**Typos must be caught, with a line number.** The contract is hand-edited in
pull requests. A silently ignored ``nullible: false`` is worse than a crash: the
check quietly stops enforcing the thing the reviewer believes it enforces. Every
unknown key is an error, and every error names its line and suggests the key the
author probably meant.

**The output has to be pleasant to read.** Fixed key order, inline short lists,
no line wrapping mid-sentence, and a header explaining what the file is for.
"""

from __future__ import annotations

import difflib
import os
from typing import Any

import yaml

from .models import (
    CONTRACT_SCHEMA_VERSION,
    Cardinality,
    ColumnContract,
    Contract,
    Defaults,
    Relationship,
    Severity,
    TableContract,
)

HEADER = """\
# Zeyvor data contract — what this data is supposed to look like.
#
# WHAT THIS FILE IS
#   A description of your data that you can check against. `zeyvor init` wrote
#   it by measuring the file you pointed it at. `zeyvor check` re-measures the
#   data and fails when it no longer matches, so a change upstream shows up as a
#   failed build rather than as a wrong number three weeks later.
#
# WHAT TO DO WITH IT
#   Read it. It describes one sample, so some of it will be wrong — a range too
#   tight, a list of values that is missing one you know exists. Correct anything
#   wrong, then commit it and review changes to it like code. The plain-English
#   line above each column says what that column currently promises.
#
# HOW TO READ A COLUMN
#   type              what kind of value: text, integer (whole numbers), float
#                     (decimals), boolean (true/false), date, timestamp
#   nullable          false means every row must have a value here
#   max_null_rate     how much of the column may be empty — 0.05 is 5%
#   unique            no two rows may share a value, as for an id
#   min / max         the range values must fall within. `today` moves with the
#                     calendar rather than going stale
#   categories        the values seen in the data
#   categories_closed true means those are the ONLY values allowed, so anything
#                     new fails the check
#   formats           the shape values take, where '#' is any digit and 'a' is
#                     any letter: '####-##-##' is a date like 2026-08-03
#   no_pii            no personal data was found here, and none is expected
#   known_issues      something already wrong in the data, recorded and accepted
#                     so it does not fail the build until you choose to fix it
#   means             what the column is for, in your own words
#
# TABLE-LEVEL SETTINGS
#   min_rows          fail if fewer rows arrive than this — catches the upstream
#                     job that silently produced an almost-empty file
#   source            the file or table this was measured from
#
# TURNING A CHECK OFF
#   Delete any line to stop checking it. Set `ignore: true` on a column to retire
#   the whole column while leaving the intent visible in review.
"""

CONTRACT_KEYS = {
    "version",
    "generated_by",
    "generated_at",
    "defaults",
    "tables",
    "relationships",
}
RELATIONSHIP_KEYS = {
    "from",
    "to",
    "cardinality",
    "max_orphan_rate",
    "means",
    "known_issues",
    "ignore",
    "on_violation",
}
DEFAULTS_KEYS = {"on_violation"}
TABLE_KEYS = {
    "source",
    "profile_fingerprint",
    "min_rows",
    "allow_new_columns",
    "allow_missing_columns",
    "on_violation",
    "columns",
}
COLUMN_KEYS = {
    "means",
    "type",
    "formats",
    "nullable",
    "max_null_rate",
    "unique",
    "categories",
    "categories_closed",
    "min",
    "max",
    "no_pii",
    "known_issues",
    "ignore",
    "on_violation",
}

COLUMN_KEY_ORDER = [
    "means",
    "type",
    "formats",
    "nullable",
    "max_null_rate",
    "unique",
    "categories",
    "categories_closed",
    "min",
    "max",
    "no_pii",
    "known_issues",
    "ignore",
    "on_violation",
]


class ContractError(ValueError):
    """A contract file that cannot be trusted to mean what it says."""


# ── line tracking ─────────────────────────────────────────────────────────────


def _line_index(text: str) -> dict[tuple[str, ...], int]:
    """Map each mapping-key path to its 1-based line number.

    Walking the composed node tree keeps line information out of the parsed data
    itself, so the loaded structure stays clean while errors stay precise.
    """
    index: dict[tuple[str, ...], int] = {}

    def walk(node: Any, path: tuple[str, ...]) -> None:
        if isinstance(node, yaml.MappingNode):
            for key_node, value_node in node.value:
                key = str(getattr(key_node, "value", ""))
                index[path + (key,)] = key_node.start_mark.line + 1
                walk(value_node, path + (key,))
        elif isinstance(node, yaml.SequenceNode):
            for item in node.value:
                walk(item, path)

    try:
        root = yaml.compose(text)
    except yaml.YAMLError:
        return index
    if root is not None:
        walk(root, ())
    return index


class _Validator:
    def __init__(self, lines: dict[tuple[str, ...], int]) -> None:
        self.lines = lines

    def at(self, path: tuple[str, ...]) -> str:
        line = self.lines.get(path)
        return f" (line {line})" if line else ""

    def check_keys(self, mapping: dict[str, Any], allowed: set[str], path: tuple[str, ...]) -> None:
        for key in mapping:
            if key in allowed:
                continue
            suggestion = difflib.get_close_matches(str(key), sorted(allowed), n=1, cutoff=0.6)
            hint = f" Did you mean '{suggestion[0]}'?" if suggestion else ""
            where = ".".join(path + (str(key),)) or str(key)
            raise ContractError(
                f"Unknown key '{key}' in {where}{self.at(path + (str(key),))}.{hint}"
            )

    def mapping(self, value: Any, path: tuple[str, ...], what: str) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ContractError(
                f"{what} must be a mapping, got {type(value).__name__}{self.at(path)}"
            )
        return value

    def string_list(self, value: Any, path: tuple[str, ...], what: str) -> list[str]:
        if value is None:
            return []
        if isinstance(value, (str, int, float)):
            return [str(value)]
        if not isinstance(value, list):
            raise ContractError(f"{what} must be a list{self.at(path)}")
        return [str(v) for v in value]

    def boolean(self, value: Any, path: tuple[str, ...], what: str) -> bool | None:
        if value is None:
            return None
        if not isinstance(value, bool):
            raise ContractError(f"{what} must be true or false, got {value!r}{self.at(path)}")
        return value

    def number(self, value: Any, path: tuple[str, ...], what: str) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ContractError(f"{what} must be a number, got {value!r}{self.at(path)}")
        return float(value)

    def severity(self, value: Any, path: tuple[str, ...]) -> Severity | None:
        if value is None:
            return None
        try:
            return Severity(str(value).lower())
        except ValueError:
            valid = ", ".join(s.value for s in Severity)
            raise ContractError(
                f"on_violation must be one of: {valid} — got {value!r}{self.at(path)}"
            ) from None


# ── loading ───────────────────────────────────────────────────────────────────


def loads(text: str) -> Contract:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        problem = getattr(exc, "problem", str(exc))
        raise ContractError(f"Could not parse the contract{where}: {problem}") from None

    if raw is None:
        raise ContractError("The contract file is empty.")
    if not isinstance(raw, dict):
        raise ContractError("The contract must be a mapping at the top level.")

    v = _Validator(_line_index(text))
    v.check_keys(raw, CONTRACT_KEYS, ())

    version = raw.get("version", CONTRACT_SCHEMA_VERSION)
    if not isinstance(version, int) or version < 1:
        raise ContractError(f"version must be a positive integer, got {version!r}")
    if version > CONTRACT_SCHEMA_VERSION:
        raise ContractError(
            f"This contract declares version {version}, but this Zeyvor "
            f"understands up to {CONTRACT_SCHEMA_VERSION}. Upgrade Zeyvor."
        )

    defaults_raw = v.mapping(raw.get("defaults"), ("defaults",), "defaults")
    v.check_keys(defaults_raw, DEFAULTS_KEYS, ("defaults",))
    defaults = Defaults(
        on_violation=v.severity(defaults_raw.get("on_violation"), ("defaults", "on_violation"))
        or Severity.FAIL
    )

    tables_raw = v.mapping(raw.get("tables"), ("tables",), "tables")
    if not tables_raw:
        raise ContractError("The contract declares no tables.")

    tables: dict[str, TableContract] = {}
    for table_name, table_body in tables_raw.items():
        path = ("tables", str(table_name))
        body = v.mapping(table_body, path, f"table '{table_name}'")
        v.check_keys(body, TABLE_KEYS, path)

        columns_raw = v.mapping(body.get("columns"), path + ("columns",), "columns")
        columns: dict[str, ColumnContract] = {}
        for column_name, column_body in columns_raw.items():
            cpath = path + ("columns", str(column_name))
            cbody = v.mapping(column_body, cpath, f"column '{column_name}'")
            v.check_keys(cbody, COLUMN_KEYS, cpath)

            categories = cbody.get("categories")
            columns[str(column_name)] = ColumnContract(
                name=str(column_name),
                means=str(cbody["means"]) if cbody.get("means") is not None else None,
                type=str(cbody["type"]) if cbody.get("type") is not None else None,
                formats=v.string_list(cbody.get("formats"), cpath + ("formats",), "formats"),
                nullable=v.boolean(cbody.get("nullable"), cpath + ("nullable",), "nullable"),
                max_null_rate=v.number(
                    cbody.get("max_null_rate"), cpath + ("max_null_rate",), "max_null_rate"
                ),
                unique=v.boolean(cbody.get("unique"), cpath + ("unique",), "unique"),
                categories=(
                    v.string_list(categories, cpath + ("categories",), "categories")
                    if categories is not None
                    else None
                ),
                categories_closed=bool(cbody.get("categories_closed", False)),
                minimum=cbody.get("min"),
                maximum=cbody.get("max"),
                no_pii=bool(cbody.get("no_pii", False)),
                known_issues=v.string_list(
                    cbody.get("known_issues"), cpath + ("known_issues",), "known_issues"
                ),
                ignore=bool(cbody.get("ignore", False)),
                on_violation=v.severity(cbody.get("on_violation"), cpath + ("on_violation",)),
            )

        min_rows = body.get("min_rows")
        if min_rows is not None and (isinstance(min_rows, bool) or not isinstance(min_rows, int)):
            raise ContractError(
                f"min_rows must be an integer, got {min_rows!r}{v.at(path + ('min_rows',))}"
            )

        tables[str(table_name)] = TableContract(
            name=str(table_name),
            source=str(body.get("source", "") or ""),
            profile_fingerprint=str(body.get("profile_fingerprint", "") or ""),
            min_rows=min_rows,
            allow_new_columns=bool(body.get("allow_new_columns", True)),
            allow_missing_columns=bool(body.get("allow_missing_columns", False)),
            columns=columns,
            on_violation=v.severity(body.get("on_violation"), path + ("on_violation",)),
        )

    return Contract(
        version=version,
        generated_by=str(raw.get("generated_by", "") or ""),
        generated_at=str(raw.get("generated_at", "") or ""),
        defaults=defaults,
        tables=tables,
        relationships=_relationships(v, raw.get("relationships"), tables),
    )


def _split_target(text: Any, what: str, where: str) -> tuple[str, str]:
    """`orders.customer_id` → ("orders", "customer_id").

    One dotted string rather than two keys: it is how a reviewer already writes a
    column, it is what the CLI prints, and it halves the number of ways to get a
    relationship half-written.
    """
    value = str(text or "").strip()
    if value.count(".") != 1 or value.startswith(".") or value.endswith("."):
        raise ContractError(f"{what} must be written table.column, got {value!r}{where}")
    table, column = value.split(".")
    return table.strip(), column.strip()


def _relationships(v: _Validator, raw: Any, tables: dict[str, TableContract]) -> list[Relationship]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ContractError(f"relationships must be a list{v.at(('relationships',))}")

    out: list[Relationship] = []
    seen: dict[str, int] = {}
    for index, item in enumerate(raw):
        path = ("relationships", str(index))
        body = v.mapping(item, path, f"relationship {index + 1}")
        v.check_keys(body, RELATIONSHIP_KEYS, path)

        where = v.at(path)
        from_table, from_column = _split_target(body.get("from"), "relationships[].from", where)
        to_table, to_column = _split_target(body.get("to"), "relationships[].to", where)

        # The child must be described in the same file, because the clause belongs
        # with the side that holds the key. The parent is checked after loading:
        # in a directory of per-table contracts it legitimately lives elsewhere.
        if from_table not in tables:
            raise ContractError(
                f"Relationship {from_table}.{from_column} -> {to_table}.{to_column} "
                f"is declared in a file that does not describe '{from_table}'. "
                f"A relationship belongs with the table holding the foreign key{where}"
            )

        cardinality_raw = str(body.get("cardinality", Cardinality.MANY_TO_ONE.value)).strip()
        try:
            cardinality = Cardinality(cardinality_raw)
        except ValueError:
            allowed = ", ".join(item.value for item in Cardinality)
            raise ContractError(
                f"cardinality must be one of {allowed}, got {cardinality_raw!r}{where}"
            ) from None

        rate = v.number(body.get("max_orphan_rate"), path + ("max_orphan_rate",), "max_orphan_rate")
        if rate is not None and not 0.0 <= rate <= 1.0:
            raise ContractError(f"max_orphan_rate is a share between 0 and 1, got {rate!r}{where}")

        relationship = Relationship(
            from_table=from_table,
            from_column=from_column,
            to_table=to_table,
            to_column=to_column,
            cardinality=cardinality,
            max_orphan_rate=rate,
            means=str(body["means"]) if body.get("means") is not None else None,
            known_issues=v.string_list(
                body.get("known_issues"), path + ("known_issues",), "known_issues"
            ),
            ignore=bool(body.get("ignore", False)),
            on_violation=v.severity(body.get("on_violation"), path + ("on_violation",)),
        )

        # The same edge twice means one of them is not doing anything, and which
        # one wins would depend on list order.
        if relationship.key in seen:
            raise ContractError(
                f"Relationship {relationship.key} is declared twice "
                f"(also at relationships[{seen[relationship.key]}]){where}"
            )
        seen[relationship.key] = index
        out.append(relationship)

    return out


def validate_relationship_targets(contract: Contract) -> None:
    """Every relationship must name columns that exist, once everything is loaded.

    Deferred to here rather than done while parsing, because a directory of
    per-table contracts is parsed one file at a time and the parent side is
    usually in a different file. A relationship pointing at nothing would
    otherwise sit in the contract doing nothing at all, which is the failure mode
    this whole package exists to complain about.
    """
    for relationship in contract.relationships:
        for table_name, column_name, side in (
            (relationship.from_table, relationship.from_column, "from"),
            (relationship.to_table, relationship.to_column, "to"),
        ):
            table = contract.tables.get(table_name)
            if table is None:
                raise ContractError(
                    f"Relationship {relationship.key} names table '{table_name}' "
                    f"({side}), which no contract describes."
                )
            if column_name not in table.columns:
                raise ContractError(
                    f"Relationship {relationship.key} names column "
                    f"'{table_name}.{column_name}' ({side}), which the contract for "
                    f"'{table_name}' does not describe."
                )


def load(path: str) -> Contract:
    """Load a contract from a file, or from a directory of per-table files.

    A dbt project with fifty models cannot share one file: every change would
    touch every reviewer, and the diff on a pull request would be unreadable. So
    a directory of `<table>.yml` is equally valid, and the single-file form stays
    the natural shape for one table.
    """
    if os.path.isdir(path):
        return load_directory(path)
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        raise ContractError(f"No contract at {path}. Create one with: zeyvor init") from None
    contract = loads(text)
    validate_relationship_targets(contract)
    return contract


def load_directory(path: str) -> Contract:
    """Merge every `*.yml` in a directory into one contract."""
    entries = sorted(name for name in os.listdir(path) if name.endswith((".yml", ".yaml")))
    if not entries:
        raise ContractError(f"No contract files in {path}. Create one with: zeyvor init")

    merged: Contract | None = None
    origin: dict[str, str] = {}
    defaults_from: str | None = None

    for name in entries:
        full = os.path.join(path, name)
        with open(full, encoding="utf-8") as handle:
            try:
                part = loads(handle.read())
            except ContractError as exc:
                # Name the file, or an error in one of fifty is a hunt.
                raise ContractError(f"{name}: {exc}") from None

        if merged is None:
            merged = part
            origin = dict.fromkeys(part.tables, name)
            if part.defaults.on_violation is not Severity.FAIL:
                defaults_from = name
            continue

        for table_name, table in part.tables.items():
            if table_name in merged.tables:
                raise ContractError(
                    f"Table '{table_name}' is described twice: "
                    f"in {origin[table_name]} and in {name}."
                )
            merged.tables[table_name] = table
            origin[table_name] = name

        declared = {relationship.key for relationship in merged.relationships}
        for relationship in part.relationships:
            if relationship.key in declared:
                raise ContractError(
                    f"{name}: relationship {relationship.key} is already declared in another file."
                )
            merged.relationships.append(relationship)
            declared.add(relationship.key)

        # Conflicting defaults across files would make behaviour depend on
        # filename order, so refuse rather than pick one.
        if part.defaults.on_violation is not Severity.FAIL:
            if defaults_from and part.defaults.on_violation is not merged.defaults.on_violation:
                raise ContractError(
                    f"{defaults_from} and {name} set different defaults.on_violation."
                )
            merged.defaults = part.defaults
            defaults_from = defaults_from or name
        merged.version = max(merged.version, part.version)

    assert merged is not None
    merged.relationships.sort(key=lambda relationship: relationship.key)
    validate_relationship_targets(merged)
    return merged


# ── dumping ───────────────────────────────────────────────────────────────────


class _Flow(list):
    """A list rendered inline, so `formats: ["####-##-##"]` stays on one line."""


class _Dumper(yaml.SafeDumper):
    def increase_indent(self, flow: bool = False, indentless: bool = False):  # noqa: D102
        # Indent block sequences under their key, which reads better in review.
        return super().increase_indent(flow, False)


_Dumper.add_representer(
    _Flow,
    lambda dumper, data: dumper.represent_sequence(
        "tag:yaml.org,2002:seq", list(data), flow_style=True
    ),
)


def _column_body(column: ColumnContract) -> dict[str, Any]:
    body: dict[str, Any] = {
        "means": column.means,
        "type": column.type,
        "formats": _Flow(column.formats) if column.formats else None,
        "nullable": column.nullable,
        "max_null_rate": column.max_null_rate,
        "unique": column.unique,
        "categories": _Flow(column.categories) if column.categories else None,
        "categories_closed": column.categories_closed or None,
        "min": column.minimum,
        "max": column.maximum,
        "no_pii": column.no_pii or None,
        "known_issues": _Flow(column.known_issues) if column.known_issues else None,
        "ignore": column.ignore or None,
        "on_violation": column.on_violation.value if column.on_violation else None,
    }
    return {key: body[key] for key in COLUMN_KEY_ORDER if body.get(key) is not None}


RELATIONSHIP_KEY_ORDER = (
    "means",
    "from",
    "to",
    "cardinality",
    "max_orphan_rate",
    "known_issues",
    "ignore",
    "on_violation",
)


def _relationship_body(relationship: Relationship) -> dict[str, Any]:
    body: dict[str, Any] = {
        "means": relationship.means,
        "from": relationship.child,
        "to": relationship.parent,
        # Written even when it is the default: which way a join fans is the whole
        # point of the clause, and a reviewer should not have to know the default.
        "cardinality": relationship.cardinality.value,
        "max_orphan_rate": relationship.max_orphan_rate,
        "known_issues": _Flow(relationship.known_issues) if relationship.known_issues else None,
        "ignore": relationship.ignore or None,
        "on_violation": relationship.on_violation.value if relationship.on_violation else None,
    }
    return {key: body[key] for key in RELATIONSHIP_KEY_ORDER if body.get(key) is not None}


# Every type the profiler can infer, so no column falls back to "Values".
_TYPE_PROSE = {
    "integer": "Whole numbers",
    "float": "Decimal numbers",
    "text": "Text",
    "boolean": "True/false values",
    "date": "Dates",
    "timestamp": "Dates with a time",
    "email": "Email addresses",
    "url": "Web addresses",
    "uuid": "Unique ids",
    "json": "JSON values",
    "mixed": "Values of more than one type",
    "empty": "Always empty",
}


def _plain_english(column: ColumnContract) -> str:
    """One sentence describing what this column's clauses actually require.

    The clause names are precise and a reviewer who does not already know them
    cannot act on the file — and a contract nobody outside the data team can read
    gets approved without being read, which is the failure this whole workflow
    exists to prevent. The sentence restates the clauses directly above them, in
    the order someone asks the questions: what is it, must it be there, what may
    it contain.
    """
    if column.ignore:
        return "Not checked."

    parts: list[str] = [_TYPE_PROSE.get(column.type or "", "Values")]

    if column.nullable is False:
        parts.append("never empty")
    elif column.max_null_rate is not None:
        parts.append(f"empty in at most {column.max_null_rate:.0%} of rows")
    elif column.nullable:
        parts.append("may be empty")

    if column.unique:
        parts.append("never repeating")

    if column.categories_closed and column.categories:
        count = len(column.categories)
        parts.append(f"and only these {count} value{'s' if count != 1 else ''}")
    else:
        if column.minimum is not None and column.maximum is not None:
            parts.append(f"between {column.minimum} and {column.maximum}")
        elif column.minimum is not None:
            parts.append(f"never below {column.minimum}")
        elif column.maximum is not None:
            parts.append(f"never above {column.maximum}")
        if column.formats:
            shapes = " or ".join(f"'{shape}'" for shape in column.formats)
            parts.append(f"shaped like {shapes}")

    return ", ".join(parts) + "."


def _annotate_columns(text: str, contract: Contract) -> str:
    """Put each column's sentence directly above it, as a YAML comment.

    Matched by position rather than by name: a column key can be any string the
    source happened to use — one survey export had `What do you think... [Gold]`,
    complete with brackets, punctuation and a trailing space — so anchoring on
    indentation and order is the only thing that holds for real files.
    """
    sentences = [
        _plain_english(column)
        for table in contract.tables.values()
        for column in table.columns.values()
    ]
    if not sentences:
        return text

    out: list[str] = []
    in_columns = False
    index = 0
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped == "columns:" and line.startswith("    ") and not line.startswith("     "):
            in_columns = True
        elif line and not line.startswith(" "):
            in_columns = False
        elif (
            in_columns
            and line.startswith("      ")
            and not line.startswith("       ")
            and index < len(sentences)
        ):
            out.append(f"      # {sentences[index]}")
            index += 1
        out.append(line)
    return "\n".join(out)


def dumps(contract: Contract, *, header: bool = True) -> str:
    payload: dict[str, Any] = {"version": contract.version}
    if contract.generated_by:
        payload["generated_by"] = contract.generated_by
    if contract.generated_at:
        payload["generated_at"] = contract.generated_at
    if contract.defaults.on_violation is not Severity.FAIL:
        payload["defaults"] = {"on_violation": contract.defaults.on_violation.value}

    tables: dict[str, Any] = {}
    for name, table in contract.tables.items():
        body: dict[str, Any] = {}
        if table.source:
            body["source"] = table.source
        if table.profile_fingerprint:
            body["profile_fingerprint"] = table.profile_fingerprint
        if table.min_rows is not None:
            body["min_rows"] = table.min_rows
        if not table.allow_new_columns:
            body["allow_new_columns"] = False
        if table.allow_missing_columns:
            body["allow_missing_columns"] = True
        if table.on_violation:
            body["on_violation"] = table.on_violation.value
        body["columns"] = {
            column_name: _column_body(column) for column_name, column in table.columns.items()
        }
        tables[name] = body
    payload["tables"] = tables

    if contract.relationships:
        payload["relationships"] = [
            _relationship_body(relationship) for relationship in contract.relationships
        ]

    text = yaml.dump(
        payload,
        Dumper=_Dumper,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=10_000,  # never wrap a `means` sentence mid-word
        indent=2,
    )
    text = _annotate_columns(text, contract)
    return (HEADER + "\n" + text) if header else text


def dump(contract: Contract, path: str, *, header: bool = True) -> None:
    """Write to a file, or to a directory of per-table files.

    A path with no YAML extension is treated as a directory, which makes
    `zeyvor init -o zeyvor/` do the obvious thing.
    """
    if _looks_like_directory(path):
        dump_directory(contract, path, header=header)
        return
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(dumps(contract, header=header))


def _looks_like_directory(path: str) -> bool:
    return (
        os.path.isdir(path) or path.endswith(("/", os.sep)) or not path.endswith((".yml", ".yaml"))
    )


def dump_directory(contract: Contract, path: str, *, header: bool = True) -> list[str]:
    """One file per table, so a change to one model touches one file."""
    os.makedirs(path, exist_ok=True)
    written: list[str] = []
    for name, table in contract.tables.items():
        single = Contract(
            version=contract.version,
            generated_by=contract.generated_by,
            generated_at=contract.generated_at,
            defaults=contract.defaults,
            tables={name: table},
            # The clause lives with the table holding the key, so one file still
            # reads as a complete statement about one table.
            relationships=[
                relationship
                for relationship in contract.relationships
                if relationship.from_table == name
            ],
        )
        safe = "".join(c if (c.isalnum() or c in "_-.") else "_" for c in name) or "table"
        target = os.path.join(path, f"{safe}.yml")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(dumps(single, header=header))
        written.append(target)
    return written


__all__ = [
    "ContractError",
    "validate_relationship_targets",
    "loads",
    "load",
    "load_directory",
    "dumps",
    "dump",
    "dump_directory",
    "HEADER",
]
