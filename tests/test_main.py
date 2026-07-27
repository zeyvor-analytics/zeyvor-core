"""Smoke tests for the development entry point.

The real command surface arrives in Part 3; this just has to be honest and not
crash while a human is using it to eyeball profiles.
"""

from __future__ import annotations

import json

from helpers import fixture_path
from zeyvor.__main__ import main


def test_summary_output(capsys):
    assert main([fixture_path("broken_dates.csv")]) == 0
    out = capsys.readouterr().out
    assert "broken_dates" in out
    assert "100 rows × 3 columns" in out
    assert "signup_date" in out
    assert "epoch_suspected" in out
    assert "fingerprint=sha256:" in out


def test_json_output_is_valid_and_complete(capsys):
    assert main([fixture_path("clean_orders.csv"), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["row_count"] == 100
    assert data["column_count"] == 7
    assert {c["name"] for c in data["columns"]} >= {"order_id", "signup_date", "status"}


def test_privacy_flag_is_honoured(capsys):
    assert main([fixture_path("clean_orders.csv"), "--json", "--privacy", "strict"]) == 0
    payload = capsys.readouterr().out
    assert "shipped" not in payload
    assert json.loads(payload)["privacy_mode"] == "strict"


def test_missing_file_reports_an_error_and_a_nonzero_exit(capsys):
    assert main(["/definitely/not/here.csv"]) == 1
    assert "error" in capsys.readouterr().err.lower()


def test_batch_size_flag_changes_nothing_but_query_count(capsys):
    main([fixture_path("wide.csv"), "--json", "--batch-size", "5"])
    small = json.loads(capsys.readouterr().out)
    main([fixture_path("wide.csv"), "--json", "--batch-size", "60"])
    large = json.loads(capsys.readouterr().out)

    assert small["query_count"] > large["query_count"]
    assert small["fingerprint"] == large["fingerprint"]
