"""Development entry point: ``python -m zeyvor <source> [--json] [--privacy MODE]``.

Deliberately minimal. The real command surface (``zeyvor init``,
``zeyvor check``) is Part 3; this exists so a human can eyeball a profile while
the engine is being built.
"""

from __future__ import annotations

import argparse
import sys

from .profile import ProfileOptions, TableProfile, profile_source


def _summarise(profile: TableProfile) -> str:
    lines = [
        f"{profile.name}  —  {profile.row_count:,} rows × {profile.column_count} columns",
        f"engine={profile.engine} dialect={profile.dialect} "
        f"queries={profile.query_count} in {profile.duration_ms}ms "
        f"privacy={profile.privacy_mode}",
        f"fingerprint={profile.fingerprint()}",
        "",
    ]
    for column in profile.columns:
        null_pct = f"{(profile_rate(column)):.0%}" if column.row_count else "—"
        header = (
            f"  {column.name:<28} {column.inferred_type.value:<10} "
            f"conf={column.type_confidence:<6.2f} nulls={null_pct:<5} "
            f"distinct={column.distinct_count}"
        )
        lines.append(header)
        if column.declared_type not in ("unknown", ""):
            lines.append(f"      declared: {column.declared_type}")
        if column.dominant_shape:
            shape = column.dominant_shape
            coverage = column.shape_coverage
            extra = f" (top shape covers {shape.rate:.0%}" if shape.rate else ""
            if coverage is not None and extra:
                extra += f", {len(column.shapes)} shapes cover {coverage:.0%})"
            elif extra:
                extra += ")"
            lines.append(f"      shape:    {shape.shape}{extra}")
        if column.enum and column.enum.complete:
            preview = ", ".join(m.value for m in column.enum.members[:6])
            more = "" if column.enum.cardinality <= 6 else f", … (+{column.enum.cardinality - 6})"
            lines.append(f"      values:   {preview}{more}")
        if column.type_mixture and len(column.type_mixture) > 1:
            mixture = "  ".join(f"{k}={v:.1%}" for k, v in column.type_mixture.items())
            lines.append(f"      mixture:  {mixture}")
        if column.pii_signals:
            lines.append(f"      pii:      {', '.join(column.pii_signals)}")
        if column.observations:
            lines.append(f"      findings: {', '.join(column.observations)}")
        lines.append("")

    if profile.warnings:
        lines.append("warnings:")
        lines.extend(f"  ! {w}" for w in profile.warnings)
    return "\n".join(lines)


def profile_rate(column) -> float:
    return (column.null_count / column.row_count) if column.row_count else 0.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m zeyvor", description="Profile a data source.")
    parser.add_argument("source", help="File, glob, URL or database URI")
    parser.add_argument("--table", help="Table name for database sources")
    parser.add_argument("--json", action="store_true", help="Emit the raw profile JSON")
    parser.add_argument(
        "--privacy",
        default="masked",
        choices=["strict", "masked", "full"],
        help="How much value-level detail may appear in the profile",
    )
    parser.add_argument("--batch-size", type=int, default=20, help="Columns per query")
    parser.add_argument("--memory-limit", help="Cap engine memory, e.g. 1GB (for CI containers)")
    parser.add_argument("--threads", type=int, help="Cap engine threads")
    args = parser.parse_args(argv)

    options = ProfileOptions(privacy=args.privacy, column_batch_size=args.batch_size)
    try:
        profile = profile_source(
            args.source,
            table=args.table,
            options=options,
            memory_limit=args.memory_limit,
            threads=args.threads,
        )
    except Exception as exc:  # pragma: no cover - developer convenience
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(profile.to_json() if args.json else _summarise(profile))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
