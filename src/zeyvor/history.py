"""What this table looked like the last hundred times we checked.

`zeyvor check` answers one question — does the data match right now — and then
forgets. That makes a whole class of failure invisible, because the failure is
not in any single run. A table that normally receives fifty thousand rows and
today received thirty thousand has violated nothing: `min_rows` is satisfied,
every column is the right type, nothing is null that should not be. Only the
comparison against yesterday says anything is wrong.

The obvious place to keep that comparison is a server, and the product does
offer one. But needing an account to notice a half-empty load is a strange
trade, and it makes the free tool look deliberately hobbled. A hundred runs of
what matters is about twenty-four kilobytes; it belongs next to the contract.

**What is stored is counts, never values.** Row count, the structural
fingerprint, and per column the number of nulls and distinct values. That is
enough for volume trends, null-rate drift, and noticing a column whose shape
keeps changing. It is deliberately not enough to reconstruct a single row —
same rule as the upload path, for the same reason: a file that accumulates on
disk should not become a slow leak of the data it is watching.

**The directory ignores itself.** A `.gitignore` containing `*` is written
alongside on creation, so history never appears in a diff unless somebody
deliberately removes it. Committing it would put a small change in every pull
request for the entire life of the repository.
"""

from __future__ import annotations

import json
import os
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone

from .profile.models import TableProfile

HISTORY_DIR = os.path.join(".zeyvor", "history")

MAX_ENTRIES = 100
"""Enough to see a month of daily runs or four days of hourly ones, and small
enough that nobody has to think about it. Pruned on write rather than by a
separate command, because a maintenance chore nobody runs is a file that grows
forever."""

MIN_ENTRIES_FOR_TREND = 3
"""Below this there is no baseline, only a couple of numbers. Comparing against
one previous run would report every ordinary Tuesday-to-Wednesday change as an
anomaly, which is the fastest way to teach somebody to ignore this."""


@dataclass(frozen=True)
class Snapshot:
    at: str
    rows: int
    fingerprint: str
    columns: dict[str, dict[str, int]]

    def to_line(self) -> str:
        return json.dumps(
            {
                "at": self.at,
                "rows": self.rows,
                "fp": self.fingerprint,
                "cols": self.columns,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_line(cls, line: str) -> Snapshot | None:
        """Tolerant by design.

        This file is appended to by whatever version of Zeyvor happened to run,
        and a reader that raises on an unfamiliar shape would turn a
        forward-compatible format into a hard failure on somebody's build. An
        unreadable line is skipped; unknown keys are ignored.
        """
        try:
            data = json.loads(line)
            return cls(
                at=str(data["at"]),
                rows=int(data["rows"]),
                fingerprint=str(data.get("fp", "")),
                columns=dict(data.get("cols", {})),
            )
        except (ValueError, KeyError, TypeError):
            return None


def _safe_name(table: str) -> str:
    cleaned = "".join(c if (c.isalnum() or c in "_-.") else "_" for c in table)
    return cleaned or "table"


def path_for(table: str, root: str = HISTORY_DIR) -> str:
    return os.path.join(root, f"{_safe_name(table)}.jsonl")


def snapshot_of(profile: TableProfile, *, at: datetime | None = None) -> Snapshot:
    moment = at or datetime.now(timezone.utc)
    return Snapshot(
        at=moment.isoformat(timespec="seconds"),
        rows=profile.row_count,
        fingerprint=profile.fingerprint(),
        columns={
            column.name: {"nulls": column.null_count, "distinct": column.distinct_count}
            for column in profile.columns
        },
    )


def read(table: str, root: str = HISTORY_DIR) -> list[Snapshot]:
    """Oldest first. A missing or unreadable file is simply no history."""
    target = path_for(table, root)
    try:
        with open(target, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    return [snap for snap in (Snapshot.from_line(line) for line in lines if line.strip()) if snap]


def append(table: str, snapshot: Snapshot, root: str = HISTORY_DIR) -> None:
    """Add one run, prune to the cap, and make sure git stays out of it.

    Never raises. History is a convenience layered onto a check that has already
    produced its verdict — a read-only filesystem or a permissions problem must
    not turn a passing build red, and the check itself needs nothing from here.
    """
    try:
        os.makedirs(root, exist_ok=True)
        _ensure_ignored(root)

        existing = read(table, root)
        entries = [*existing, snapshot][-MAX_ENTRIES:]
        target = path_for(table, root)
        # Written whole rather than appended so pruning happens in the same
        # step; at a hundred short lines the cost is not worth a second path.
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("\n".join(entry.to_line() for entry in entries) + "\n")
    except OSError:
        return


def _ensure_ignored(root: str) -> None:
    """A `.gitignore` of `*` beside the history, written once.

    Self-ignoring rather than editing the repository's root `.gitignore`, which
    is a file somebody else owns and may have strong opinions about.
    """
    parent = os.path.dirname(root) or root
    marker = os.path.join(parent, ".gitignore")
    if os.path.exists(marker):
        return
    try:
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write(
                "# Zeyvor keeps per-run counts here so it can spot trends a single\n"
                "# run cannot show. Ignored because committing it would put a small\n"
                "# change in every pull request forever. Delete this file if you\n"
                "# would rather share the history across CI runners.\n"
                "*\n"
            )
    except OSError:
        return


@dataclass(frozen=True)
class VolumeTrend:
    """How today's row count compares with the recent normal."""

    current: int
    baseline: int
    runs: int

    @property
    def drop(self) -> float:
        """Fraction below baseline. 0.4 means "40% fewer rows than usual"."""
        if self.baseline <= 0:
            return 0.0
        return max(0.0, (self.baseline - self.current) / self.baseline)


def volume_trend(history: list[Snapshot], current_rows: int) -> VolumeTrend | None:
    """Compare a row count against the median of recent runs.

    Median rather than mean, because the thing being guarded against is exactly
    the kind of outlier that would drag a mean toward itself: one catastrophic
    load in the window should not quietly lower the bar for the next one.
    """
    if len(history) < MIN_ENTRIES_FOR_TREND:
        return None
    counts = [snap.rows for snap in history]
    baseline = int(statistics.median(counts))
    if baseline <= 0:
        return None
    return VolumeTrend(current=current_rows, baseline=baseline, runs=len(counts))


__all__ = [
    "HISTORY_DIR",
    "MAX_ENTRIES",
    "MIN_ENTRIES_FOR_TREND",
    "Snapshot",
    "VolumeTrend",
    "append",
    "path_for",
    "read",
    "snapshot_of",
    "volume_trend",
]
