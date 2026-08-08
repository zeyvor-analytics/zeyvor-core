"""In-repo history, and the trend it makes possible.

The point of this file is a failure a single run structurally cannot see: a
table that usually loads fifty thousand rows and today loaded thirty thousand
has violated nothing. `min_rows` is satisfied, every column is the right type,
nothing is null that should not be. Only the comparison against previous runs
says anything is wrong.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from zeyvor import history
from zeyvor.profile.models import ColumnProfile, TableProfile


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return os.path.join(".zeyvor", "history")


def _snap(rows: int, days_ago: int = 0) -> history.Snapshot:
    at = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return history.Snapshot(
        at=at.isoformat(timespec="seconds"), rows=rows, fingerprint="fp", columns={}
    )


# ── writing ───────────────────────────────────────────────────────────────────


def test_a_run_is_remembered(root):
    history.append("orders", _snap(100), root)
    assert [s.rows for s in history.read("orders", root)] == [100]


def test_history_ignores_itself(root):
    """Committing it would put a small change in every pull request forever."""
    history.append("orders", _snap(100), root)
    marker = os.path.join(".zeyvor", ".gitignore")
    assert os.path.exists(marker)
    with open(marker, encoding="utf-8") as handle:
        assert "*" in handle.read()


def test_only_the_last_hundred_runs_are_kept(root):
    for i in range(130):
        history.append("orders", _snap(100 + i), root)
    kept = history.read("orders", root)
    assert len(kept) == history.MAX_ENTRIES
    assert kept[-1].rows == 229, "the newest run survives"


def test_writing_never_raises(root, monkeypatch):
    """History is a convenience layered onto a verdict already reached. A
    read-only filesystem must not turn a passing build red."""

    def explode(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr("os.makedirs", explode)
    history.append("orders", _snap(100), root)  # must not raise


def test_an_unreadable_line_is_skipped_not_fatal(root):
    history.append("orders", _snap(100), root)
    with open(history.path_for("orders", root), "a", encoding="utf-8") as handle:
        handle.write("not json\n{}\n")
    assert [s.rows for s in history.read("orders", root)] == [100]


def test_no_history_is_simply_no_history(root):
    assert history.read("never_seen", root) == []


def test_awkward_table_names_do_not_escape_the_directory(root):
    """A table name reaches this as a filename, and a warehouse will happily
    hold one with a slash in it. What matters is not that the name is pretty
    but that the resolved path stays inside the history directory — `..` as
    literal characters in a single filename is harmless; a path separator is
    not.
    """
    history.append("analytics/../../etc/passwd", _snap(1), root)

    written = os.listdir(root)
    assert len(written) == 1
    assert os.sep not in written[0]

    resolved = os.path.realpath(history.path_for("analytics/../../etc/passwd", root))
    assert resolved.startswith(os.path.realpath(root) + os.sep)


# ── the trend ─────────────────────────────────────────────────────────────────


def test_a_baseline_needs_more_than_a_couple_of_runs():
    """Comparing against one previous run would report every ordinary
    Tuesday-to-Wednesday change as an anomaly."""
    assert history.volume_trend([_snap(100)], 50) is None
    assert history.volume_trend([_snap(100), _snap(100)], 50) is None
    assert history.volume_trend([_snap(100)] * 3, 50) is not None


def test_a_drop_is_measured_against_the_median():
    """Median, not mean: one catastrophic load in the window must not quietly
    lower the bar for the next one."""
    past = [_snap(50_000), _snap(50_000), _snap(50_000), _snap(1)]
    trend = history.volume_trend(past, 30_000)
    assert trend.baseline == 50_000
    assert trend.drop == pytest.approx(0.4)


def test_growth_is_not_a_drop():
    trend = history.volume_trend([_snap(100)] * 3, 500)
    assert trend.drop == 0


def test_a_snapshot_records_counts_and_never_values():
    """Same rule as the upload path: a file that accumulates on disk must not
    become a slow leak of the data it is watching."""
    column = ColumnProfile(name="email", row_count=10, null_count=2, distinct_count=8)
    profile = TableProfile(name="users", row_count=10, columns=[column])

    line = history.snapshot_of(profile).to_line()

    assert '"rows":10' in line
    assert '"nulls":2' in line
    for leak in ("minimum", "maximum", "categories", "sample", "example"):
        assert leak not in line
