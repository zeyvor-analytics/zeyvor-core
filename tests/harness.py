"""Running the mutations, and counting what came back.

Deliberately a subprocess per mutation rather than calling `main()` in-process.
`init` and `check` both touch module-level state and the working directory, and
a recall number contaminated by the previous mutation's leftovers would be worse
than no number at all — it would be a confident wrong one.

History is switched off for the same reason. A single `check` cannot trip a
volume trend anyway, but leaving it on would make the results depend on
execution order, and results that depend on order are not measurements.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass

from mutations import MUTATIONS, Mutation, read_table, write_table

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLEAN = os.path.join(REPO, "tests", "fixtures", "clean_orders.csv")


@dataclass
class Outcome:
    mutation: str
    expected: tuple[str, ...]
    found: tuple[str, ...]
    caught: bool
    """Whether any expected type appeared. The unit of recall."""

    collateral: tuple[str, ...]
    """Findings beyond the expected ones. One upstream change should produce one
    message; this is the number that says whether cascade suppression works."""

    exit_code: int
    error: str = ""

    @property
    def clean_catch(self) -> bool:
        return self.caught and not self.collateral


def _run(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "zeyvor", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        # `text=True` alone decodes with the platform default, which is cp1252
        # on a Windows runner and would mangle the mojibake mutation into a
        # different kind of broken than the one being tested. CI escalates
        # EncodingWarning to an error precisely to catch this.
        encoding="utf-8",
        env={**os.environ, "PYTHONPATH": os.path.join(REPO, "src"), "ANTHROPIC_API_KEY": ""},
    )


def run_mutation(mutation: Mutation, clean_csv: str = CLEAN) -> Outcome:
    """init against clean data, check against broken data — the real sequence."""
    work = tempfile.mkdtemp(prefix=f"zv_{mutation.name}_")
    try:
        target = os.path.join(work, "orders.csv")
        table = read_table(clean_csv)

        if mutation.prepare is not None:
            table = mutation.prepare(table)
        write_table(target, table)

        born = _run(["init", "orders.csv", "--no-ai"], work)
        if born.returncode != 0:
            return Outcome(
                mutation.name,
                mutation.expects,
                (),
                False,
                (),
                born.returncode,
                error=f"init failed: {born.stderr.strip()[:200]}",
            )

        if mutation.contract_patch is not None:
            contract = os.path.join(work, "zeyvor.yml")
            with open(contract, encoding="utf-8") as handle:
                original = handle.read()
            patched = mutation.contract_patch(original)
            if patched == original:
                return Outcome(
                    mutation.name,
                    mutation.expects,
                    (),
                    False,
                    (),
                    0,
                    error="contract_patch matched nothing — the clause was not applied",
                )
            with open(contract, "w", encoding="utf-8") as handle:
                handle.write(patched)

        # Re-read, so `prepare` is part of the baseline rather than the breakage.
        before = read_table(target)
        after = mutation.apply(read_table(target))

        # A mutation that changes nothing produces no findings, which is
        # indistinguishable from a detection failure unless it is caught here.
        # The first version of `numeric_as_text` formatted values under 1,000
        # and inserted no separator at all; it scored as a miss for two runs.
        if after == before:
            return Outcome(
                mutation.name,
                mutation.expects,
                (),
                False,
                (),
                0,
                error="mutation left the data unchanged — broken mutation, not a missed finding",
            )
        write_table(target, after)

        result = _run(["check", "--format", "json", "--quiet", "--no-history"], work)
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return Outcome(
                mutation.name,
                mutation.expects,
                (),
                False,
                (),
                result.returncode,
                error=f"unreadable check output: {result.stdout[:120]}",
            )

        found = tuple(sorted({v["type"] for v in payload.get("violations", [])}))
        caught = any(e in found for e in mutation.expects)
        collateral = tuple(f for f in found if f not in mutation.expects)
        return Outcome(
            mutation.name, mutation.expects, found, caught, collateral, result.returncode
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)


def run_all() -> list[Outcome]:
    return [run_mutation(m) for m in MUTATIONS]


def report(outcomes: list[Outcome]) -> str:
    caught = sum(1 for o in outcomes if o.caught)
    total = len(outcomes)
    noisy = [o for o in outcomes if o.caught and len(o.collateral) > 2]

    lines = [
        "",
        "  DRIFT INJECTION — RECALL",
        "  " + "─" * 74,
        f"  {'mutation':<26} {'caught':<7} {'expected':<24} collateral",
        "  " + "─" * 74,
    ]
    for o in sorted(outcomes, key=lambda x: (x.caught, x.mutation)):
        mark = "yes" if o.caught else "NO"
        extra = f"+{len(o.collateral)}" if o.collateral else "—"
        expected = (
            o.expected[0] if len(o.expected) == 1 else f"{o.expected[0]} (+{len(o.expected) - 1})"
        )
        lines.append(f"  {o.mutation:<26} {mark:<7} {expected:<24} {extra}")
        if o.error:
            lines.append(f"      ! {o.error}")
    # Recall means nothing without saying recall *of what*. A perfect score
    # across a third of the finding types would be a misleading headline, and
    # this is the line that stops it becoming one.
    from zeyvor.contract.violations import ViolationType

    exercised = {e for o in outcomes for e in o.expected if o.caught}
    every = {v.value for v in ViolationType}
    untested = sorted(every - exercised)

    lines += [
        "  " + "─" * 74,
        f"  recall {caught}/{total} ({caught / total * 100:.0f}%)"
        f"    mutations producing >2 extra findings: {len(noisy)}",
        f"  finding types exercised {len(exercised)}/{len(every)}",
        "",
    ]
    if untested:
        lines.append("  not exercised by any mutation:")
        for name in untested:
            lines.append(f"    {name}")
        lines.append("")
    for o in outcomes:
        if not o.caught:
            lines.append(f"  MISSED {o.mutation}: expected {o.expected}, got {o.found or '()'}")
    return "\n".join(lines)


if __name__ == "__main__":
    outcomes = run_all()
    print(report(outcomes))
    sys.exit(0 if all(o.caught for o in outcomes) else 1)
