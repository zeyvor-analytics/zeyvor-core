"""Reporting a run to a Zeyvor account.

The tests that matter here are the negative ones. This is the only feature that
sends anything off the machine, so the interesting question is never "did the
payload arrive" but "is there any way a value from the data ends up in it".

`test_no_data_value_ever_reaches_the_payload` is the one to keep working. It
builds a report whose violations are stuffed with recognisable values in every
field a violation has, serialises the payload, and asserts none of those strings
survive. If someone later adds a field to `build_payload` that carries prose,
that test fails — which is the point of asserting on the serialised JSON rather
than on the fields we happen to know about today.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from zeyvor.contract.violations import Report, Severity, Violation, ViolationType
from zeyvor.integrations.upload import (
    SCHEMA_VERSION,
    TOKEN_ENV,
    UploadError,
    build_payload,
    git_context,
    post_report,
    safe_metrics,
)

# Strings a payload must never contain. Every one of these is planted somewhere
# in the report the fixture below builds.
SECRETS = [
    "awaiting_pickup",
    "jane@example.com",
    "2019-01-01",
    "ACME-SECRET-VALUE",
]


@pytest.fixture
def report() -> Report:
    """A report deliberately contaminated with values in every prose field."""
    return Report(
        violations=[
            Violation(
                type=ViolationType.NEW_CATEGORY,
                table="orders",
                column="status",
                severity=Severity.FAIL,
                expected="one of [pending, shipped, delivered]",
                found="1 new value: 'awaiting_pickup' (12 rows)",
                detail="ACME-SECRET-VALUE appeared upstream",
                remedy="Accept it or fix the source",
                evidence={"new_categories": ["awaiting_pickup"], "distinct": 4},
            ),
            Violation(
                type=ViolationType.TYPE_CONTAMINATED,
                table="orders",
                column="signup_date",
                severity=Severity.WARN,
                expected="date",
                found="97% date, 3% integer",
                detail="jane@example.com is in there too",
                evidence={
                    "expected_type": "date",
                    "mixture": {"date": 0.97, "integer": 0.03},
                    "observed_min": "2019-01-01",
                    "contamination": 0.03,
                },
            ),
        ],
        tables_checked=1,
        columns_checked=7,
        checked_at="2026-07-27T12:00:00Z",
        zeyvor_version="0.1.0",
    )


# ── the guarantee ─────────────────────────────────────────────────────────────


def test_no_data_value_ever_reaches_the_payload(report):
    body = json.dumps(build_payload(report, project="acme/warehouse", git={}))
    for secret in SECRETS:
        assert secret not in body, f"{secret!r} leaked into the upload payload"


def test_payload_carries_no_prose_fields(report):
    payload = build_payload(report, project="acme/warehouse", git={})
    for finding in payload["findings"]:
        assert set(finding) == {"type", "table", "column", "severity", "metrics"}


def test_structure_and_counts(report):
    payload = build_payload(report, project="acme/warehouse", git={})

    assert payload["schema"] == SCHEMA_VERSION
    assert payload["project"] == "acme/warehouse"
    assert payload["run"]["failed"] == 1
    assert payload["run"]["warned"] == 1
    assert payload["run"]["ok"] is False
    assert payload["run"]["columns_checked"] == 7
    assert payload["run"]["zeyvor_version"] == "0.1.0"
    assert len(payload["findings"]) == 2


def test_names_are_kept_because_a_history_needs_them(report):
    payload = build_payload(report, project="acme/warehouse", git={})
    columns = {f["column"] for f in payload["findings"]}
    assert columns == {"status", "signup_date"}
    assert all(f["table"] == "orders" for f in payload["findings"])


# ── metric reduction ──────────────────────────────────────────────────────────


def test_numbers_survive():
    assert safe_metrics({"null_rate": 0.12, "duplicates": 3}) == {
        "null_rate": 0.12,
        "duplicates": 3,
    }


def test_value_lists_become_counts():
    assert safe_metrics({"new_categories": ["a", "b", "c"]}) == {"new_categories_count": 3}


def test_observed_extremes_are_dropped():
    """They are literal values out of rows, whatever their type."""
    assert safe_metrics({"observed_min": "2019-01-01", "observed_max": 99999}) == {}


def test_type_labels_survive():
    assert safe_metrics({"expected_type": "date", "found_type": "integer"}) == {
        "expected_type": "date",
        "found_type": "integer",
    }


def test_shapes_survive_because_they_are_already_anonymous():
    assert safe_metrics({"shape": "####-##-##"}) == {"shape": "####-##-##"}


def test_arbitrary_strings_do_not_survive():
    assert safe_metrics({"note": "customer jane@example.com complained"}) == {}


def test_nested_mixtures_keep_only_numbers():
    got = safe_metrics({"mixture": {"date": 0.97, "integer": 0.03, "sample": "2024-01-01"}})
    assert got == {"mixture": {"date": 0.97, "integer": 0.03}}


def test_empty_evidence_is_fine():
    assert safe_metrics({}) == {}


# ── transport ─────────────────────────────────────────────────────────────────


def test_missing_token_is_explained_not_guessed(monkeypatch):
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    with pytest.raises(UploadError, match=TOKEN_ENV):
        post_report({"schema": 1}, endpoint="https://example.invalid/api")


def test_unreachable_endpoint_names_the_endpoint(monkeypatch):
    def explode(*_args, **_kwargs):
        raise urllib.error.URLError("nodename nor servname provided")

    monkeypatch.setattr("urllib.request.urlopen", explode)
    with pytest.raises(UploadError, match="example.invalid"):
        post_report({"schema": 1}, endpoint="https://example.invalid/api", token="t")


def test_server_error_message_is_surfaced(monkeypatch):
    def explode(*_args, **_kwargs):
        raise urllib.error.HTTPError(
            "https://example.invalid/api",
            403,
            "Forbidden",
            {},  # type: ignore[arg-type]
            None,
        )

    monkeypatch.setattr("urllib.request.urlopen", explode)
    with pytest.raises(UploadError, match="403"):
        post_report({"schema": 1}, endpoint="https://example.invalid/api", token="t")


def test_token_is_sent_as_a_bearer_header(monkeypatch):
    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def capture(request, timeout=None):  # noqa: ARG001
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.data)
        captured["url"] = request.full_url
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", capture)
    post_report(
        {"schema": 1, "run": {"zeyvor_version": "0.1.0"}}, endpoint="https://x/api", token="s3cret"
    )

    # urllib title-cases header names.
    assert captured["headers"]["Authorization"] == "Bearer s3cret"
    assert captured["body"]["schema"] == 1
    assert captured["url"] == "https://x/api"


def test_endpoint_comes_from_the_environment_when_not_passed(monkeypatch):
    monkeypatch.setenv("ZEYVOR_ENDPOINT", "https://self-hosted.example/api/reports")
    monkeypatch.setenv(TOKEN_ENV, "t")
    seen = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def capture(request, timeout=None):  # noqa: ARG001
        seen["url"] = request.full_url
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", capture)
    post_report({"schema": 1})
    assert seen["url"] == "https://self-hosted.example/api/reports"


# ── git context ───────────────────────────────────────────────────────────────


def test_ci_environment_wins_over_a_detached_checkout(monkeypatch):
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    monkeypatch.setenv("GITHUB_REF_NAME", "main")
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/warehouse")
    assert git_context() == {"sha": "a" * 40, "ref": "main", "repo": "acme/warehouse"}


def test_git_context_survives_no_git_at_all(monkeypatch):
    for var in (
        "GITHUB_SHA",
        "GITHUB_REF_NAME",
        "GITHUB_REPOSITORY",
        "CI_COMMIT_SHA",
        "CI_COMMIT_REF_NAME",
        "CI_PROJECT_PATH",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("zeyvor.integrations.upload._git", lambda *_: "")
    assert git_context() == {}
