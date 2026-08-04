"""`python -m zeyvor` must behave exactly like the `zeyvor` command.

Once these were two implementations and the module printed its own profile
summary; now it is a shim, so all that needs verifying is that the shim is real
and that the module actually runs.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from helpers import fixture_path


def _env() -> dict[str, str]:
    """The current environment plus a PYTHONPATH pointing at `src`.

    Deliberately inherited rather than replaced. This used to pass
    `{"PYTHONPATH": ..., "PATH": "/usr/bin:/bin"}`, which wiped everything else
    — and on Windows that removes `SystemRoot`, without which the interpreter
    cannot seed its hash randomisation and dies before running a line:

        Fatal Python error: _Py_HashRandomization_Init

    The minimal PATH was never load-bearing either; it is a POSIX path list, so
    it meant nothing on Windows. What these tests are actually checking is that
    `-m zeyvor` resolves from a different working directory, and PYTHONPATH is
    the only variable that has any bearing on that.
    """
    return {**os.environ, "PYTHONPATH": _src_dir()}


def test_module_entry_is_the_cli():
    from zeyvor.__main__ import main as module_main
    from zeyvor.cli.main import main as cli_main

    assert module_main is cli_main


def test_running_the_module_works(tmp_path):
    """Spawned for real, because `-m` resolution is exactly what could break."""
    result = subprocess.run(
        [sys.executable, "-m", "zeyvor", "profile", fixture_path("clean_orders.csv"), "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=tmp_path,
        env=_env(),
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["row_count"] == 100


def test_module_help_lists_the_commands(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "zeyvor"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=tmp_path,
        env=_env(),
    )
    assert result.returncode == 0
    assert "zeyvor check" in result.stdout


def _src_dir() -> str:
    import zeyvor

    return os.path.dirname(os.path.dirname(os.path.abspath(zeyvor.__file__)))


def test_the_two_version_strings_agree():
    """`generated_by` is stamped into every contract from __version__, while PyPI
    publishes what pyproject says. If they drift, a contract records a version
    that was never released and the mismatch is invisible until someone tries to
    reproduce a result.
    """
    import pathlib
    import re

    import zeyvor

    pyproject = pathlib.Path(__file__).resolve().parent.parent / "pyproject.toml"
    declared = re.search(r'^version = "([^"]+)"', pyproject.read_text(encoding="utf-8"), re.M)

    assert declared, "no version in pyproject.toml"
    assert declared.group(1) == zeyvor.__version__
