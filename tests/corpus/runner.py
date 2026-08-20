"""Measuring what Zeyvor says about data nobody broke on purpose.

Phase 1 measured recall: break something known, check it is caught. That says
nothing about the failure mode people actually abandon a tool over, which is
being told a hundred things are wrong when nothing is. This measures that.

Two stages, answering different questions.

**Stage A — the invariant.** `init` a dataset, then `check` the *same
untouched bytes*. A generated contract is supposed to always pass against the
data it was generated from; `generate.py` says so in its opening paragraph and
the fixtures test it. Any finding here is a hard bug, not a judgement call, and
running it against ninety unfamiliar datasets is the point — fixtures were
written by someone who knew what the profiler does.

**Stage B — the false-positive rate.** Contract on the first half of a dataset,
check against the second half. Nothing is broken on purpose. Both halves are
legitimate data from the same source, which is the closest honest analogue of
"yesterday's contract, today's data".

Stage B needs a caveat stated plainly rather than buried: a finding there is
not automatically wrong. Real datasets genuinely drift within themselves — a
category that only appears in later rows is a real new category, and a tool
that stayed silent about it would be failing. So the number this prints is an
upper bound on the false-positive rate, and the per-type breakdown is there so
a human can see which checks are doing the complaining before anybody publishes
a figure.
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
MANIFEST = os.path.join(HERE, "manifest.json")
CACHE = os.path.join(REPO, ".zeyvor-corpus")

# Datasets this narrow cannot be split into two halves that mean anything: a
# contract built on twenty rows asserts almost nothing, so a clean result would
# be evidence of nothing rather than evidence of precision.
MIN_ROWS_FOR_SPLIT = 40


@dataclass
class Result:
    name: str
    rows: int = 0
    columns: int = 0

    same_data_findings: tuple[str, ...] = ()
    """Stage A. Anything here is a bug."""

    split_findings: tuple[str, ...] = ()
    """Stage B. An upper bound on false positives, not a verdict."""

    skipped: str = ""
    error: str = ""

    @property
    def usable(self) -> bool:
        return not self.skipped and not self.error


def load_manifest() -> list[dict]:
    with open(MANIFEST, encoding="utf-8") as handle:
        return json.load(handle)["datasets"]


def fetch(entry: dict) -> str:
    """Download once, keep forever. Re-running must not re-hammer GitHub."""
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, entry["name"].replace("/", "__") + ".csv")
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    request = urllib.request.Request(entry["url"], headers={"User-Agent": "zeyvor-corpus"})
    with urllib.request.urlopen(request, timeout=120) as response:
        data = response.read()
    with open(path, "wb") as handle:
        handle.write(data)
    return path


def _zeyvor(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "zeyvor", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
        env={**os.environ, "PYTHONPATH": os.path.join(REPO, "src"), "ANTHROPIC_API_KEY": ""},
    )


def _findings(result: subprocess.CompletedProcess) -> tuple[str, ...] | None:
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return tuple(sorted(v["type"] for v in payload.get("violations", [])))


def _read_rows(path: str) -> tuple[list[str], list[list[str]]]:
    with open(path, newline="", encoding="utf-8", errors="replace") as handle:
        rows = list(csv.reader(handle))
    return rows[0], rows[1:]


def _write_rows(path: str, header: list[str], rows: list[list[str]]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def run_one(entry: dict) -> Result:
    name = entry["name"]
    try:
        source = fetch(entry)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return Result(name, error=f"fetch failed: {exc}")

    try:
        header, rows = _read_rows(source)
    except (csv.Error, IndexError, UnicodeDecodeError) as exc:
        return Result(name, skipped=f"unreadable csv: {exc}")

    if not header or not rows:
        return Result(name, skipped="empty")

    result = Result(name, rows=len(rows), columns=len(header))
    work = tempfile.mkdtemp(prefix="zvcorpus_")
    try:
        # ── Stage A: contract on the data, check the same data ────────────────
        target = os.path.join(work, "data.csv")
        _write_rows(target, header, rows)

        born = _zeyvor(["init", "data.csv", "--no-ai"], work)
        if born.returncode != 0:
            return Result(
                name, result.rows, result.columns, error=f"init failed: {born.stderr.strip()[:160]}"
            )

        same = _zeyvor(["check", "--format", "json", "--quiet", "--no-history"], work)
        found = _findings(same)
        if found is None:
            return Result(
                name,
                result.rows,
                result.columns,
                error=f"unreadable check output: {same.stdout[:120]}",
            )
        result.same_data_findings = found

        # ── Stage B: contract on the first half, check the second ─────────────
        if len(rows) < MIN_ROWS_FOR_SPLIT:
            result.skipped = f"only {len(rows)} rows, too few to split"
            return result

        middle = len(rows) // 2
        shutil.rmtree(work, ignore_errors=True)
        work = tempfile.mkdtemp(prefix="zvcorpus_")
        target = os.path.join(work, "data.csv")

        _write_rows(target, header, rows[:middle])
        born = _zeyvor(["init", "data.csv", "--no-ai"], work)
        if born.returncode != 0:
            result.error = f"split init failed: {born.stderr.strip()[:160]}"
            return result

        _write_rows(target, header, rows[middle:])
        later = _zeyvor(["check", "--format", "json", "--quiet", "--no-history"], work)
        split = _findings(later)
        result.split_findings = () if split is None else split
        return result
    except subprocess.TimeoutExpired:
        return Result(name, result.rows, result.columns, error="timed out")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def report(results: list[Result]) -> str:
    usable = [r for r in results if r.usable]
    failed = [r for r in results if r.error]
    skipped = [r for r in results if r.skipped and not r.error]

    dirty = [r for r in usable if r.same_data_findings]
    split_ran = [r for r in usable if r.rows >= MIN_ROWS_FOR_SPLIT]
    noisy = [r for r in split_ran if r.split_findings]

    types_a: Counter = Counter()
    for r in dirty:
        types_a.update(r.same_data_findings)
    types_b: Counter = Counter()
    for r in noisy:
        types_b.update(r.split_findings)

    lines = [
        "",
        "  CORPUS — FALSE POSITIVES ON REAL DATA",
        "  " + "═" * 72,
        f"  {len(results)} datasets in the manifest",
        f"    usable            {len(usable)}",
        f"    fetch/run failed  {len(failed)}",
        f"    skipped           {len(skipped)}",
        "",
        "  STAGE A — contract checked against the data it was generated from",
        "  " + "─" * 72,
        "  A generated contract must pass against its own data. Any finding is a bug.",
        "",
        f"    clean          {len(usable) - len(dirty)}/{len(usable)}",
        f"    with findings  {len(dirty)}",
    ]
    if types_a:
        lines.append("")
        for kind, count in types_a.most_common():
            lines.append(f"      {kind:<28} {count}")
        lines.append("")
        for r in dirty[:12]:
            lines.append(f"      {r.name:<44} {', '.join(r.same_data_findings)}")

    lines += [
        "",
        "  STAGE B — contract on the first half, checked against the second",
        "  " + "─" * 72,
        "  Nothing was broken on purpose. Some findings here will be real drift",
        "  inside the dataset, so this is an upper bound rather than a verdict.",
        "",
        f"    silent         {len(split_ran) - len(noisy)}/{len(split_ran)}",
        f"    with findings  {len(noisy)}",
    ]
    if split_ran:
        rate = len(noisy) / len(split_ran) * 100
        lines.append(f"    upper-bound false-positive rate  {rate:.1f}%")
    if types_b:
        lines.append("")
        lines.append("    which checks did the complaining:")
        for kind, count in types_b.most_common():
            lines.append(f"      {kind:<28} {count}")

    if failed:
        lines += ["", "  failures:"]
        for r in failed[:10]:
            lines.append(f"    {r.name:<44} {r.error[:70]}")
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    entries = load_manifest()
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else len(entries)
    entries = entries[:limit]

    results = []
    for index, entry in enumerate(entries, 1):
        print(f"  [{index}/{len(entries)}] {entry['name']}", file=sys.stderr, flush=True)
        results.append(run_one(entry))

    print(report(results))
    with open(os.path.join(HERE, "last-run.json"), "w", encoding="utf-8") as handle:
        json.dump(
            [
                {
                    "name": r.name,
                    "rows": r.rows,
                    "columns": r.columns,
                    "same_data": list(r.same_data_findings),
                    "split": list(r.split_findings),
                    "skipped": r.skipped,
                    "error": r.error,
                }
                for r in results
            ],
            handle,
            indent=2,
        )
        handle.write("\n")
