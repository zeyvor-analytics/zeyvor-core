"""`python -m zeyvor` must behave exactly like the `zeyvor` command.

Once these were two implementations and the module printed its own profile
summary; now it is a shim, so all that needs verifying is that the shim is real
and that the module actually runs.
"""

from __future__ import annotations

import json
import subprocess
import sys

from helpers import fixture_path


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
        env={"PYTHONPATH": _src_dir(), "PATH": "/usr/bin:/bin"},
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
        env={"PYTHONPATH": _src_dir(), "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0
    assert "zeyvor check" in result.stdout


def _src_dir() -> str:
    import os

    import zeyvor

    return os.path.dirname(os.path.dirname(os.path.abspath(zeyvor.__file__)))
