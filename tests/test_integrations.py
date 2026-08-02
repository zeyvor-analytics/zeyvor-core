"""dbt manifests, published output, and Slack.

The dbt path is tested against a committed manifest fixture and a DuckDB
database standing in for the warehouse, so it runs offline and without dbt
installed. Publishing is tested as pure functions, because that is what they are.
"""

from __future__ import annotations

import json

import pytest

from helpers import fixture_path
from zeyvor.integrations.dbt import (
    DbtError,
    DbtModel,
    load_manifest,
    manifest_version,
    models,
    sources_for,
)
from zeyvor.integrations.publish import (
    MARKER,
    post_to_slack,
    redact_violation,
    to_markdown,
    to_slack_blocks,
)


@pytest.fixture
def manifest():
    return load_manifest(fixture_path("dbt_manifest.json"))


# ── dbt: reading the manifest ─────────────────────────────────────────────────


def test_models_are_found(manifest):
    found = {m.name for m in models(manifest)}
    assert found == {"orders", "customers", "country_codes"}


def test_ephemeral_models_are_skipped(manifest):
    """An ephemeral model is inlined as a CTE — there is no table to check."""
    assert "int_helper" not in {m.name for m in models(manifest)}


def test_tests_and_other_nodes_are_skipped(manifest):
    assert "not_null_orders_id" not in {m.name for m in models(manifest)}


def test_seeds_are_included(manifest):
    """A seed is a real table, and a seed drifting is worth catching."""
    assert "country_codes" in {m.name for m in models(manifest)}


def test_the_alias_is_used_not_the_model_name(manifest):
    """Checking the wrong table would be a silent no-op."""
    customers = next(m for m in models(manifest) if m.name == "customers")
    assert customers.alias == "dim_customers"
    assert customers.relation.endswith("analytics.dim_customers")


def test_selection_narrows(manifest):
    assert {m.name for m in models(manifest, select=["orders"])} == {"orders"}


def test_selecting_a_model_that_does_not_exist_lists_what_does(manifest):
    with pytest.raises(DbtError) as excinfo:
        models(manifest, select=["ordrs"])
    message = str(excinfo.value)
    assert "ordrs" in message
    assert "orders" in message, "the available models should be offered"


def test_manifest_version_is_reported(manifest):
    assert "v12" in manifest_version(manifest)


def test_a_missing_manifest_points_at_dbt():
    with pytest.raises(DbtError) as excinfo:
        load_manifest("/nope/target/manifest.json")
    assert "dbt compile" in str(excinfo.value)


def test_a_manifest_from_the_future_or_the_past_is_read_defensively():
    """Field drift across dbt releases must not be fatal."""
    sparse = {"metadata": {}, "nodes": {"model.a.x": {"resource_type": "model", "name": "x"}}}
    found = models(sparse)
    assert found[0].name == "x"
    assert found[0].alias == "x"  # alias defaults to the name
    assert manifest_version(sparse) == "unknown"


def test_something_that_is_not_a_manifest_says_so(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"hello": "world"}), encoding="utf-8")
    with pytest.raises(DbtError, match="nodes"):
        models(load_manifest(str(path)))


# ── dbt: turning models into sources ──────────────────────────────────────────


def test_source_uris_carry_the_connection_and_the_table(manifest):
    pairs = dict(sources_for(manifest, "snowflake://ACME"))
    assert pairs["orders"] == "snowflake://ACME#warehouse.analytics.orders"


def test_local_backends_get_a_two_part_name(manifest):
    """DuckDB and Postgres already know which database they are in; a three-part
    name would not resolve."""
    pairs = dict(sources_for(manifest, "duckdb:///wh.duckdb"))
    assert pairs["orders"] == "duckdb:///wh.duckdb#analytics.orders"


def test_a_warehouse_is_required(manifest):
    with pytest.raises(DbtError) as excinfo:
        sources_for(manifest, "")
    assert "--warehouse" in str(excinfo.value)


def test_a_warehouse_with_a_fragment_is_rejected():
    model = DbtModel(name="x", database="d", schema="s", alias="x")
    with pytest.raises(DbtError, match="fragment"):
        model.source_uri("duckdb:///wh.duckdb#already.here")


# ── publishing: the privacy rule ──────────────────────────────────────────────


SAMPLE = {
    "ok": False,
    "failed": 1,
    "warned": 1,
    "tables_checked": 1,
    "columns_checked": 7,
    "violations": [
        {
            "type": "new_category",
            "severity": "fail",
            "table": "orders",
            "column": "status",
            "expected": "one of 'pending', 'shipped'",
            "found": "1 new value: 'awaiting_pickup' (12 rows)",
            "remedy": "Add the value to categories if it is legitimate.",
            "evidence": {"new_categories": ["awaiting_pickup"]},
        },
        {
            "type": "mojibake_appeared",
            "severity": "warn",
            "table": "orders",
            "column": "notes",
            "found": "2 of 100 rows (2.0%) contain mis-decoded characters",
            "remedy": "Read and write UTF-8 end to end.",
        },
    ],
}


def test_published_output_omits_values_by_default():
    """A comment on a public pull request is a publication; a terminal is not."""
    markdown = to_markdown(SAMPLE)
    assert "awaiting_pickup" not in markdown
    assert "value(s) not in the contract" in markdown
    assert "Values are omitted" in markdown


def test_values_can_be_opted_back_in():
    markdown = to_markdown(SAMPLE, show_values=True)
    assert "awaiting_pickup" in markdown
    assert "Values are omitted" not in markdown


def test_redaction_keeps_every_number():
    """Redaction must cost detail, not evidence."""
    redacted = redact_violation(SAMPLE["violations"][0])
    assert "awaiting_pickup" not in json.dumps(redacted)
    assert "1 value(s)" in redacted["found"]
    assert redacted["type"] == "new_category"


def test_findings_without_values_are_untouched():
    warning = SAMPLE["violations"][1]
    assert redact_violation(warning)["found"] == warning["found"]


# ── publishing: markdown ──────────────────────────────────────────────────────


def test_markdown_carries_the_marker_for_comment_updates():
    """Without it, every push would add another comment."""
    assert to_markdown(SAMPLE).startswith(MARKER)


def test_markdown_leads_with_the_verdict():
    markdown = to_markdown(SAMPLE)
    assert "❌ **Failed**" in markdown
    assert "1 failed, 1 warned" in markdown


def test_a_clean_run_is_short():
    clean = {
        "ok": True,
        "failed": 0,
        "warned": 0,
        "tables_checked": 2,
        "columns_checked": 9,
        "violations": [],
    }
    markdown = to_markdown(clean)
    assert "✅" in markdown
    assert "9 columns" in markdown
    assert "<details>" not in markdown, "nothing to expand when nothing is wrong"


def test_pipes_in_values_do_not_break_the_table():
    report = dict(SAMPLE)
    report["violations"] = [{**SAMPLE["violations"][0], "found": "a | b | c"}]
    row = next(
        line for line in to_markdown(report, show_values=True).splitlines() if "| ❌ |" in line
    )
    # The pipes from the value are escaped, so they do not become cell breaks.
    import re

    assert "a \\| b \\| c" in row
    cells = [c for c in re.split(r"(?<!\\)\|", row) if c.strip()]
    assert len(cells) == 4, f"expected 4 cells, got {cells}"


def test_newlines_are_flattened_into_cells():
    report = dict(SAMPLE)
    report["violations"] = [{**SAMPLE["violations"][0], "found": "line one\nline two"}]
    assert "line one line two" in to_markdown(report, show_values=True)


# ── publishing: slack ─────────────────────────────────────────────────────────


def test_slack_payload_shape():
    payload = to_slack_blocks(SAMPLE)
    assert payload["blocks"][0]["type"] == "header"
    assert "1 failed" in payload["text"]
    assert all("type" in block for block in payload["blocks"])


def test_slack_redacts_by_default():
    assert "awaiting_pickup" not in json.dumps(to_slack_blocks(SAMPLE))
    assert "awaiting_pickup" in json.dumps(to_slack_blocks(SAMPLE, show_values=True))


def test_slack_leads_with_failures():
    """Slack truncates, so the important half must come first."""
    payload = to_slack_blocks(SAMPLE)
    body = json.dumps(payload["blocks"][1:])
    assert body.index("new_category") < body.index("mojibake")


def test_slack_caps_a_long_report():
    many = {
        **SAMPLE,
        "failed": 40,
        "violations": [{**SAMPLE["violations"][0], "column": f"c{i}"} for i in range(40)],
    }
    payload = to_slack_blocks(many)
    assert len(payload["blocks"]) <= 13
    assert "30 more findings" in json.dumps(payload["blocks"][-1])


def test_slack_success_message():
    clean = {
        "ok": True,
        "failed": 0,
        "warned": 0,
        "tables_checked": 1,
        "columns_checked": 5,
        "violations": [],
    }
    assert "✅" in to_slack_blocks(clean)["text"]


def test_a_slack_failure_is_reported_not_swallowed(monkeypatch):
    def refuse(*args, **kwargs):
        import urllib.error

        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr("urllib.request.urlopen", refuse)
    with pytest.raises(RuntimeError, match="Could not reach Slack"):
        post_to_slack("https://hooks.slack.test/x", {"text": "hi"})
