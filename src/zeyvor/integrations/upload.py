"""Sending a run to a Zeyvor account, so the history outlives the terminal.

`zeyvor check` answers one question — does the data match the contract right
now — and then forgets. It cannot tell you that a column has failed four of the
last thirty runs, or that someone loosened a rule last Tuesday. That needs
somewhere to remember, which means a server, which means this module.

Everything here is shaped by one problem: the product's central promise is that
your rows never leave your machine, and this is the one feature that sends
anything anywhere. So the guarantee is made structural rather than careful.

**No string that came from your data is sent.** Not redacted, not truncated —
not collected. The payload carries a finding's *type*, its table and column
names, its severity, and numbers. The dashboard writes its own English from
those, which it has to do anyway to format and localise. There is no
`--show-values` escape hatch here, unlike a PR comment, because a comment is
read by people you chose and a server is a third party holding a copy.

Consequences worth stating plainly:

- Table and column *names* are sent. They have to be — a history keyed on
  anonymous ids would be unreadable. Names can themselves be sensitive
  (`patients_hiv_positive`), so `--upload` is opt-in and off by default.
- Uploading is best-effort. A failed upload never changes the exit code: a
  network problem at the reporting service must not turn a passing build red,
  and must not hide a real failure either.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from typing import Any

SCHEMA_VERSION = 1

DEFAULT_ENDPOINT = "https://zeyvor.com/api/reports"

TOKEN_ENV = "ZEYVOR_TOKEN"
ENDPOINT_ENV = "ZEYVOR_ENDPOINT"
PROJECT_ENV = "ZEYVOR_PROJECT"

# A value shape: digits collapsed to #, letters to a. Safe to send because it is
# already the anonymised form — `####-##-##` cannot be read back as a date.
_SHAPE = re.compile(r"^[#a\W_]+$")

# Strings that describe the contract or the inference, not the data: a type
# name, a mixture family. Every other string is dropped.
_SAFE_STRING_KEYS = frozenset({"expected_type", "found_type", "observation"})

_METRIC_KEYS_TO_DROP = frozenset(
    {
        # Literal values lifted out of rows. The reason this module exists is to
        # not send these.
        "new_categories",
        "missing_categories",
        "offending",
        "observed_min",
        "observed_max",
        "expected_max",
        "expected_min",
    }
)


def safe_metrics(evidence: dict[str, Any]) -> dict[str, Any]:
    """Reduce a violation's evidence to numbers and safe labels.

    Anything carrying a value from the data is dropped. A list of values becomes
    its length, because "three new categories appeared" is the useful part and
    which three is not the server's business.
    """
    out: dict[str, Any] = {}
    for key, value in (evidence or {}).items():
        if key in _METRIC_KEYS_TO_DROP:
            if isinstance(value, list):
                out[f"{key}_count"] = len(value)
            continue
        if isinstance(value, (bool, int, float)):
            out[key] = value
        elif isinstance(value, str):
            if key in _SAFE_STRING_KEYS or _SHAPE.match(value):
                out[key] = value
        elif isinstance(value, dict):
            # Mixtures and signal counts: {"date": 0.97, "integer": 0.03}.
            nested = {k: v for k, v in value.items() if isinstance(v, (int, float))}
            if nested:
                out[key] = nested
        elif isinstance(value, list):
            out[f"{key}_count"] = len(value)
    return out


def git_context() -> dict[str, str]:
    """Whatever the surrounding CI or checkout will tell us, best-effort.

    CI environment variables are preferred over shelling out: in a GitHub Action
    the checkout is often detached, so `git rev-parse --abbrev-ref HEAD` says
    "HEAD" while the environment knows the real branch.
    """
    context: dict[str, str] = {}

    sha = os.environ.get("GITHUB_SHA") or os.environ.get("CI_COMMIT_SHA")
    ref = os.environ.get("GITHUB_REF_NAME") or os.environ.get("CI_COMMIT_REF_NAME")
    repo = os.environ.get("GITHUB_REPOSITORY") or os.environ.get("CI_PROJECT_PATH")

    if not sha:
        sha = _git("rev-parse", "HEAD")
    if not ref:
        branch = _git("rev-parse", "--abbrev-ref", "HEAD")
        ref = branch if branch and branch != "HEAD" else ""

    if sha:
        context["sha"] = sha[:40]
    if ref:
        context["ref"] = ref
    if repo:
        context["repo"] = repo
    return context


def _git(*args: str) -> str:
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def build_payload(
    report: Any, *, project: str, git: dict[str, str] | None = None
) -> dict[str, Any]:
    """Assemble the upload body from a check report.

    `report` is a `contract.violations.Report`; it is typed loosely to keep this
    module free of a circular import.
    """
    findings = [
        {
            "type": violation.type.value,
            "table": violation.table,
            "column": violation.column,
            "severity": violation.severity.value,
            "metrics": safe_metrics(violation.evidence),
        }
        for violation in report.violations
    ]

    return {
        "schema": SCHEMA_VERSION,
        "project": project,
        "run": {
            "at": report.checked_at,
            "zeyvor_version": report.zeyvor_version,
            "ok": report.ok,
            "tables_checked": report.tables_checked,
            "columns_checked": report.columns_checked,
            "failed": len(report.failures),
            "warned": len(report.warnings),
            "git": git if git is not None else git_context(),
        },
        "findings": findings,
    }


class UploadError(RuntimeError):
    """Raised when a report could not be delivered. Never fatal to a check."""


def post_report(
    payload: dict[str, Any],
    *,
    endpoint: str | None = None,
    token: str | None = None,
    timeout: float = 10.0,
) -> None:
    """POST the payload, using the standard library only.

    The token comes from the environment rather than a flag: a flag lands in
    shell history and in the echoed command line of every CI log.
    """
    endpoint = endpoint or os.environ.get(ENDPOINT_ENV) or DEFAULT_ENDPOINT
    token = token or os.environ.get(TOKEN_ENV) or ""
    if not token:
        raise UploadError(
            f"No upload token. Set {TOKEN_ENV} to the token from your project's settings."
        )

    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 - endpoint is user-configured by design
        endpoint,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": f"zeyvor/{payload.get('run', {}).get('zeyvor_version', '?')}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            if response.status >= 300:
                raise UploadError(f"{endpoint} returned {response.status}")
    except urllib.error.HTTPError as exc:
        detail = _error_detail(exc)
        raise UploadError(f"{endpoint} returned {exc.code}{detail}") from None
    except urllib.error.URLError as exc:
        raise UploadError(f"Could not reach {endpoint}: {exc.reason}") from None


def _error_detail(exc: urllib.error.HTTPError) -> str:
    """Surface the server's own explanation when it bothered to give one."""
    try:
        body = exc.read().decode("utf-8", "replace")[:200]
    except Exception:  # noqa: BLE001 - the error path must not raise
        return ""
    if not body:
        return ""
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict) and parsed.get("error"):
            return f" — {parsed['error']}"
    except json.JSONDecodeError:
        pass
    return f" — {body.strip()}"


__all__ = [
    "DEFAULT_ENDPOINT",
    "ENDPOINT_ENV",
    "PROJECT_ENV",
    "SCHEMA_VERSION",
    "TOKEN_ENV",
    "UploadError",
    "build_payload",
    "git_context",
    "post_report",
    "safe_metrics",
]
