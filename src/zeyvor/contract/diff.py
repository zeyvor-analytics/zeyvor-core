"""Checking a profile against a contract.

Entirely deterministic and offline: no model, no network, no API key. A check
runs on every push, so it has to be free, instant, and give the same answer
twice.

Two ideas do most of the work here.

**Compatibility, not equality.** A contract asking for a float is satisfied by
integers; one asking for text is satisfied by emails. Only a narrowing of
meaning is a violation. Comparing types for equality would produce constant
noise on data that is behaving perfectly well.

**Cascade suppression.** When a column's type has changed outright, its format,
range and category clauses are all meaningless — reporting them too would bury
the finding that matters under four that follow from it. One problem should
produce one story.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..profile.models import InferredType, Observation, TableProfile
from .models import ColumnContract, Contract, Severity, TableContract
from .violations import (
    DEFAULT_SEVERITY,
    Report,
    Violation,
    ViolationType,
    humanise_count,
    quote_list,
)

CONTAMINATION_FLOOR = 0.001
"""One value in a thousand. Deliberately sensitive: a handful of foreign values
in an otherwise clean column is a breaking upstream change on its first day,
which is the earliest and cheapest moment to catch it."""

UNIT_SHIFT_FACTOR = 10.0
"""A maximum this far past its bound is reported as a probable unit change
rather than a range breach, because the remedy is completely different."""

TEXTISH = {
    InferredType.TEXT.value,
    InferredType.EMAIL.value,
    InferredType.URL.value,
    InferredType.UUID.value,
    InferredType.JSON.value,
}

TEMPORAL = {InferredType.DATE.value, InferredType.TIMESTAMP.value}

TYPE_LABEL = {
    "integer": "whole numbers",
    "float": "numbers",
    "boolean": "true/false values",
    "date": "calendar dates",
    "timestamp": "timestamps",
    "email": "email addresses",
    "url": "URLs",
    "uuid": "UUIDs",
    "json": "JSON values",
    "text": "text",
    "mixed": "a mixture of types",
    "empty": "no values at all",
}


def label(type_name: str | None) -> str:
    return TYPE_LABEL.get(type_name or "", type_name or "unspecified")


def type_accepts(contracted: str, found: str) -> bool:
    """Does `found` still honour a contract asking for `contracted`?"""
    if contracted == found:
        return True
    if contracted == InferredType.FLOAT.value and found == InferredType.INTEGER.value:
        return True
    if contracted == InferredType.TIMESTAMP.value and found == InferredType.DATE.value:
        return True
    if contracted == InferredType.TEXT.value and found in TEXTISH:
        return True
    return False


# A profile's type *mixture* is coarser than its inferred type: it partitions
# values into integer / float / date / timestamp / text only. Emails, UUIDs,
# JSON and booleans all land in the text residual. Contamination therefore has
# to be judged at family granularity — comparing a fine-grained contracted type
# against a coarse mixture family reported an email column as 100% contaminated.
MIXTURE_FAMILY = {
    InferredType.INTEGER.value: "integer",
    InferredType.FLOAT.value: "float",
    InferredType.DATE.value: "date",
    InferredType.TIMESTAMP.value: "timestamp",
    InferredType.BOOLEAN.value: "text",
    InferredType.EMAIL.value: "text",
    InferredType.URL.value: "text",
    InferredType.UUID.value: "text",
    InferredType.JSON.value: "text",
    InferredType.TEXT.value: "text",
}

# Which mixture families satisfy a contract of each family. Text sits at the top
# of the lattice: a text contract promises nothing about content, so anything
# satisfies it, and drift in a text column is caught by the format and pattern
# clauses instead.
FAMILY_ACCEPTS: dict[str, set[str]] = {
    "text": {"text", "integer", "float", "date", "timestamp"},
    "float": {"float", "integer"},
    "integer": {"integer"},
    "timestamp": {"timestamp", "date"},
    "date": {"date"},
}


def family_accepts(contracted: str, found_family: str) -> bool:
    family = MIXTURE_FAMILY.get(contracted, "text")
    return found_family in FAMILY_ACCEPTS.get(family, {"text"})


def _contamination(contracted: str, mixture: dict[str, float]) -> tuple[float, dict[str, float]]:
    """Share of values whose type the contract does not accept."""
    offending = {
        family: share
        for family, share in mixture.items()
        if share > 0 and not family_accepts(contracted, family)
    }
    return sum(offending.values()), offending


def _describe_mixture(mixture: dict[str, float]) -> str:
    ranked = sorted(mixture.items(), key=lambda kv: kv[1], reverse=True)
    return ", ".join(f"{share:.1%} {family}" for family, share in ranked if share > 0)


def _resolve_bound(value, *, temporal: bool, now: datetime):
    """Resolve `today`/`now` tokens; return a comparable value or None."""
    if value is None:
        return None
    if isinstance(value, str):
        token = value.strip().lower()
        if token == "today":
            return now.date().isoformat()
        if token == "now":
            return now.isoformat(timespec="seconds")
    if temporal:
        text = str(value)
        return text.split("T")[0].split(" ")[0] if len(text) >= 10 else text
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _date_part(iso: str | None) -> str | None:
    if not iso:
        return None
    return iso.split("T")[0].split(" ")[0]


class _Checker:
    def __init__(self, contract: Contract, now: datetime) -> None:
        self.contract = contract
        self.now = now

    def severity(
        self,
        vtype: ViolationType,
        table: TableContract | None = None,
        column: ColumnContract | None = None,
    ) -> Severity:
        return self.contract.resolve_severity(DEFAULT_SEVERITY[vtype], table, column)

    def add(
        self,
        out: list[Violation],
        vtype: ViolationType,
        table: TableContract,
        column: ColumnContract | None = None,
        **kwargs,
    ) -> None:
        severity = self.severity(vtype, table, column)
        if severity is Severity.IGNORE:
            return
        out.append(
            Violation(
                type=vtype,
                table=table.name,
                column=column.name if column else None,
                severity=severity,
                **kwargs,
            )
        )

    # ── table level ───────────────────────────────────────────────────────────

    def check_table(self, profile: TableProfile, table: TableContract) -> list[Violation]:
        out: list[Violation] = []

        if table.min_rows is not None and profile.row_count < table.min_rows:
            self.add(
                out,
                ViolationType.ROW_COUNT_BELOW_MIN,
                table,
                expected=f"at least {table.min_rows:,} rows",
                found=f"{profile.row_count:,} rows",
                detail="A collapse in volume usually means an upstream job "
                "failed or a filter changed.",
                remedy="Check the job that produces this table.",
                evidence={"row_count": profile.row_count, "min_rows": table.min_rows},
            )

        profile_columns = {c.name for c in profile.columns}
        for name, column in table.columns.items():
            if name not in profile_columns and not table.allow_missing_columns:
                self.add(
                    out,
                    ViolationType.COLUMN_MISSING,
                    table,
                    column,
                    expected=f"a column named '{name}'",
                    found="the column is absent",
                    detail=(f"Contract says: {column.means}" if column.means else ""),
                    remedy="Restore the column, rename the clause, or set "
                    "allow_missing_columns: true.",
                )

        if not table.allow_new_columns:
            for column_profile in profile.columns:
                if column_profile.name not in table.columns:
                    self.add(
                        out,
                        ViolationType.COLUMN_ADDED,
                        table,
                        expected="no columns beyond those in the contract",
                        found=f"a new column '{column_profile.name}' "
                        f"({label(column_profile.inferred_type.value)})",
                        remedy=f"Add '{column_profile.name}' to the contract, or set "
                        "allow_new_columns: true.",
                    )
        return out

    # ── column level ──────────────────────────────────────────────────────────

    def check_column(
        self, column_profile, column: ColumnContract, table: TableContract
    ) -> list[Violation]:
        out: list[Violation] = []
        if not column.is_checked():
            return out

        self._check_type(out, column_profile, column, table)
        self._check_format(out, column_profile, column, table)
        self._check_categories(out, column_profile, column, table)
        self._check_presence(out, column_profile, column, table)
        self._check_uniqueness(out, column_profile, column, table)
        self._check_range(out, column_profile, column, table)
        self._check_pii(out, column_profile, column, table)
        self._check_observations(out, column_profile, column, table)

        return _suppress_cascade(out)

    def _check_type(self, out, profile, column, table) -> None:
        contracted = column.type
        if not contracted:
            return
        found = profile.inferred_type.value

        # An emptied column is a presence problem, reported by the null checks.
        if found == InferredType.EMPTY.value:
            return

        if found == InferredType.MIXED.value:
            share = sum(
                v for k, v in profile.type_mixture.items() if family_accepts(contracted, k)
            )
            if share >= 0.5:
                self._add_contamination(out, profile, column, table, contracted)
            else:
                self.add(
                    out,
                    ViolationType.TYPE_CHANGED,
                    table,
                    column,
                    expected=label(contracted),
                    found=_describe_mixture(profile.type_mixture) or label(found),
                    detail="No single type accounts for this column any more.",
                    remedy=f"Fix the source, or change type to match what the data "
                    f"now holds.",
                    evidence={"expected_type": contracted, "mixture": profile.type_mixture},
                )
            return

        if not type_accepts(contracted, found):
            self.add(
                out,
                ViolationType.TYPE_CHANGED,
                table,
                column,
                expected=label(contracted),
                found=f"{label(found)} ({profile.type_confidence:.0%} confidence)",
                detail=(f"Contract says: {column.means}" if column.means else ""),
                remedy=f"Fix the source, or set type: {found} if this change is intended.",
                evidence={"expected_type": contracted, "found_type": found},
            )
            return

        self._add_contamination(out, profile, column, table, contracted)

    def _add_contamination(self, out, profile, column, table, contracted: str) -> None:
        share, offending = _contamination(contracted, profile.type_mixture)
        if share < CONTAMINATION_FLOOR or not offending:
            return

        affected = int(round(share * profile.valued_count))
        shapes = ", ".join(
            f"{s.shape} ({s.count:,})" for s in profile.shapes[:3] if s.shape
        )
        self.add(
            out,
            ViolationType.TYPE_CONTAMINATED,
            table,
            column,
            expected=label(contracted)
            + (f" ({quote_list(column.formats)})" if column.formats else ""),
            found=f"{_describe_mixture(profile.type_mixture)} — "
            f"{humanise_count(affected, profile.valued_count)} do not fit",
            detail=(f"Shapes present: {shapes}" if shapes else ""),
            remedy="Fix the source. If the new values are legitimate, widen the "
            "contract to accept them.",
            evidence={
                "expected_type": contracted,
                "contamination": round(share, 6),
                "offending": {k: round(v, 6) for k, v in offending.items()},
            },
        )

    def _check_format(self, out, profile, column, table) -> None:
        if not column.formats or not profile.shapes:
            return
        allowed = set(column.formats)
        # Any shape with a non-trivial share that the contract does not allow.
        intruders = [
            s
            for s in profile.shapes
            if s.shape and s.shape not in allowed and (s.rate or 0) >= 0.01
        ]
        if not intruders:
            return
        self.add(
            out,
            ViolationType.FORMAT_CHANGED,
            table,
            column,
            expected=f"values shaped {quote_list(column.formats)}",
            found=", ".join(
                f"'{s.shape}' ({humanise_count(s.count, profile.valued_count)})"
                for s in intruders[:3]
            ),
            detail="'#' stands for a digit and 'a' for a letter.",
            remedy="Fix the source, or add the new shape to formats.",
            evidence={
                "allowed": list(column.formats),
                "found": [s.shape for s in intruders],
            },
        )

    def _check_categories(self, out, profile, column, table) -> None:
        if column.categories is None or not column.categories_closed:
            return
        enum = profile.enum
        if enum is None or not enum.complete:
            self.add(
                out,
                ViolationType.CATEGORIES_UNVERIFIABLE,
                table,
                column,
                expected=f"one of {quote_list(column.categories)}",
                found=f"{profile.distinct_count:,} distinct values — too many to "
                "enumerate, so membership could not be checked",
                remedy="Raise max_enum_cardinality when profiling, or drop "
                "categories_closed for this column.",
            )
            return

        contracted = set(column.categories)
        present = set(enum.values())
        new = sorted(present - contracted)
        gone = sorted(contracted - present)

        if new:
            counts = {m.value: m.count for m in enum.members}
            self.add(
                out,
                ViolationType.NEW_CATEGORY,
                table,
                column,
                expected=f"one of {quote_list(column.categories)}",
                found=f"{len(new)} new value{'s' if len(new) != 1 else ''}: "
                + ", ".join(f"'{v}' ({counts.get(v, 0):,} rows)" for v in new[:5]),
                detail=(f"Contract says: {column.means}" if column.means else ""),
                remedy="Add the value to categories if it is legitimate, or fix "
                "the source that produced it.",
                evidence={"new_categories": new},
            )
        if gone:
            self.add(
                out,
                ViolationType.CATEGORY_DISAPPEARED,
                table,
                column,
                expected=f"all of {quote_list(column.categories)} to appear",
                found=f"missing: {quote_list(gone)}",
                detail="Not necessarily wrong — a category can simply be unused "
                "in this window.",
                remedy="Remove it from categories if it is retired.",
                evidence={"missing_categories": gone},
            )

    def _check_presence(self, out, profile, column, table) -> None:
        if column.nullable is False and profile.null_count > 0:
            self.add(
                out,
                ViolationType.NULLABILITY_VIOLATED,
                table,
                column,
                expected="no missing values",
                found=humanise_count(profile.null_count, profile.row_count) + " are null",
                detail=(f"Contract says: {column.means}" if column.means else ""),
                remedy="Fix the source, or allow a tolerance with "
                f"max_null_rate: {max(round((profile.null_rate or 0) + 0.01, 3), 0.01)}.",
                evidence={"null_count": profile.null_count, "null_rate": profile.null_rate},
            )
        elif column.max_null_rate is not None:
            rate = profile.null_rate or 0.0
            if rate > column.max_null_rate:
                self.add(
                    out,
                    ViolationType.NULL_RATE_EXCEEDED,
                    table,
                    column,
                    expected=f"at most {column.max_null_rate:.1%} missing",
                    found=f"{rate:.1%} missing "
                    f"({humanise_count(profile.null_count, profile.row_count)})",
                    remedy="Fix the source, or raise max_null_rate.",
                    evidence={"null_rate": rate, "max_null_rate": column.max_null_rate},
                )

    def _check_uniqueness(self, out, profile, column, table) -> None:
        if column.unique is not True or profile.is_unique:
            return
        if profile.distinct_is_approx:
            return  # an approximate count cannot prove duplication
        duplicates = max(profile.non_null_count - profile.distinct_count, 0)
        self.add(
            out,
            ViolationType.UNIQUENESS_LOST,
            table,
            column,
            expected="every value distinct",
            found=f"{duplicates:,} duplicate value{'s' if duplicates != 1 else ''} "
            f"({profile.distinct_count:,} distinct across "
            f"{profile.non_null_count:,} rows)",
            detail="Downstream joins on this column will fan out.",
            remedy="Deduplicate at the source, or drop unique: true.",
            evidence={"duplicates": duplicates, "distinct": profile.distinct_count},
        )

    def _check_range(self, out, profile, column, table) -> None:
        temporal = (column.type or "") in TEMPORAL
        low = _resolve_bound(column.minimum, temporal=temporal, now=self.now)
        high = _resolve_bound(column.maximum, temporal=temporal, now=self.now)
        if low is None and high is None:
            return

        if temporal:
            if not profile.temporal:
                return
            observed_low = _date_part(profile.temporal.minimum)
            observed_high = _date_part(profile.temporal.maximum)
        else:
            if not profile.numeric:
                return
            observed_low = profile.numeric.minimum
            observed_high = profile.numeric.maximum

        breaches: list[str] = []
        if low is not None and observed_low is not None and observed_low < low:
            breaches.append(f"minimum {observed_low} is below {low}")
        if high is not None and observed_high is not None and observed_high > high:
            breaches.append(f"maximum {observed_high} is above {high}")
        if not breaches:
            return

        # A maximum an order of magnitude past its bound is far more likely to be
        # a unit change than a genuine outlier, and the remedy is different.
        if (
            not temporal
            and high is not None
            and observed_high is not None
            and high > 0
            and observed_high >= high * UNIT_SHIFT_FACTOR
        ):
            factor = observed_high / high
            self.add(
                out,
                ViolationType.UNIT_SHIFT_SUSPECTED,
                table,
                column,
                expected=f"values between {low} and {high}",
                found=f"maximum {observed_high:,.2f} — about {factor:.0f}x the "
                "expected ceiling",
                detail="Values of this magnitude usually mean the unit changed "
                "(dollars to cents, seconds to milliseconds) rather than a "
                "genuine outlier. The type has not changed, so nothing else "
                "would catch this.",
                remedy="Confirm the unit at the source before trusting any "
                "aggregate over this column.",
                evidence={"observed_max": observed_high, "expected_max": high, "factor": factor},
            )
            return

        self.add(
            out,
            ViolationType.RANGE_EXCEEDED,
            table,
            column,
            expected=f"values between {low if low is not None else '−∞'} and "
            f"{high if high is not None else '∞'}",
            found="; ".join(breaches),
            remedy="Fix the source, or widen min/max.",
            evidence={"observed_min": observed_low, "observed_max": observed_high},
        )

    def _check_pii(self, out, profile, column, table) -> None:
        if not column.no_pii:
            return
        signals = profile.pii_signals
        if not signals:
            return
        counts = {s: profile.pattern_hits.get(s, 0) for s in signals}
        worst = max(counts.values()) if counts else 0
        self.add(
            out,
            ViolationType.PII_APPEARED,
            table,
            column,
            expected="no personal data",
            found=", ".join(f"{name} ({count:,} rows)" for name, count in counts.items()),
            detail="This column is not declared as holding personal data, so it is "
            "unlikely to be covered by whatever handles PII downstream.",
            remedy="Remove the data at the source, or drop no_pii and treat this "
            "column as sensitive everywhere it is used.",
            evidence={"signals": counts, "worst_count": worst},
        )

    def _check_observations(self, out, profile, column, table) -> None:
        """Report Part 1 findings that were not already accepted in the contract."""
        known = set(column.known_issues)
        mapping = [
            (Observation.EPOCH_SUSPECTED, ViolationType.EPOCH_SUSPECTED,
             "Unix timestamps have appeared in a column of dates.",
             "Convert at the source, or widen the contract if the change is intended."),
            (Observation.EXCEL_SERIAL_SUSPECTED, ViolationType.EXCEL_SERIAL_SUSPECTED,
             "Values look like Excel serial dates (a spreadsheet round-trip).",
             "Export as ISO dates rather than through a spreadsheet."),
            (Observation.NULL_WORDS, ViolationType.NULL_WORDS_APPEARED,
             "Text such as 'N/A' or '-' is standing in for missing data, so every "
             "null check counts these rows as present.",
             "Write real nulls at the source."),
            (Observation.MOJIBAKE, ViolationType.MOJIBAKE_APPEARED,
             "Mis-decoded characters indicate a broken encoding step.",
             "Read and write UTF-8 end to end."),
            (Observation.MIXED_BOOLEAN_ENCODING, ViolationType.MIXED_BOOLEAN_ENCODING,
             "A true/false column is spelled several ways at once.",
             "Normalise to one vocabulary at the source."),
        ]
        for observation, vtype, detail, remedy in mapping:
            if not profile.has(observation) or observation.value in known:
                continue
            self.add(
                out,
                vtype,
                table,
                column,
                expected="this was not present when the contract was written",
                found=observation.value.replace("_", " "),
                detail=detail,
                remedy=remedy,
                evidence={"observation": observation.value},
            )


def _suppress_cascade(violations: list[Violation]) -> list[Violation]:
    """One problem, one story.

    A changed type makes the format, range and category clauses moot; reporting
    them as well would bury the finding that matters under its own consequences.
    """
    types = {v.type for v in violations}
    suppressed: set[ViolationType] = set()

    if ViolationType.TYPE_CHANGED in types:
        suppressed |= {
            ViolationType.TYPE_CONTAMINATED,
            ViolationType.FORMAT_CHANGED,
            ViolationType.NEW_CATEGORY,
            ViolationType.RANGE_EXCEEDED,
            ViolationType.UNIT_SHIFT_SUSPECTED,
            ViolationType.CATEGORIES_UNVERIFIABLE,
        }
    elif ViolationType.TYPE_CONTAMINATED in types:
        suppressed |= {ViolationType.FORMAT_CHANGED}

    if ViolationType.UNIT_SHIFT_SUSPECTED in types:
        suppressed |= {ViolationType.RANGE_EXCEEDED}

    return [v for v in violations if v.type not in suppressed]


# ── public API ────────────────────────────────────────────────────────────────


def check(
    profiles: TableProfile | list[TableProfile],
    contract: Contract,
    *,
    now: datetime | None = None,
) -> Report:
    """Compare measured profiles against a contract.

    >>> report = check(profile, contract)
    >>> report.exit_code
    0
    """
    if isinstance(profiles, TableProfile):
        profiles = [profiles]
    now = now or datetime.now(timezone.utc)

    checker = _Checker(contract, now)
    by_name = {p.name: p for p in profiles}
    violations: list[Violation] = []
    columns_checked = 0
    tables_checked = 0

    for name, table in contract.tables.items():
        profile = by_name.get(name)
        if profile is None:
            severity = checker.severity(ViolationType.TABLE_MISSING, table)
            if severity is not Severity.IGNORE:
                violations.append(
                    Violation(
                        type=ViolationType.TABLE_MISSING,
                        table=name,
                        severity=severity,
                        expected=f"a table named '{name}'",
                        found="no profile was supplied for it",
                        remedy="Profile the table, or remove it from the contract.",
                    )
                )
            continue

        tables_checked += 1
        violations.extend(checker.check_table(profile, table))

        for column_name, column in table.columns.items():
            column_profile = profile.get(column_name)
            if column_profile is None or not column.is_checked():
                continue
            columns_checked += 1
            violations.extend(checker.check_column(column_profile, column, table))

    from .. import __version__

    return Report(
        violations=violations,
        tables_checked=tables_checked,
        columns_checked=columns_checked,
        checked_at=now.isoformat(timespec="seconds"),
        zeyvor_version=__version__,
    )


__all__ = ["check", "type_accepts", "CONTAMINATION_FLOOR", "UNIT_SHIFT_FACTOR"]
