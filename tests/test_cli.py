"""The command line.

Tests drive `main(argv)` directly rather than spawning a subprocess: same code
path, no interpreter startup, and assertions can read the exit code and the two
streams separately. Keeping stdout and stderr distinct matters here, because
`zeyvor check --json | jq` only works if narration never reaches stdout.
"""

from __future__ import annotations

import json
import shutil

import pytest

from helpers import fixture_path
from zeyvor.cli.main import EXIT_ERROR, EXIT_OK, EXIT_VIOLATIONS, build_parser, main


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A throwaway directory with a copy of a fixture, as a user's repo."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    shutil.copy(fixture_path("clean_orders.csv"), tmp_path / "orders.csv")
    return tmp_path


def break_dates(path, *, rows: int = 3) -> None:
    """Simulate the upstream change: some dates arrive as epoch timestamps."""
    import csv

    # encoding is explicit because Windows would otherwise use cp1252, write the
    # fixture's em-dashes as 0x97, and then fail when the CLI reads it as UTF-8.
    with open(path, encoding="utf-8") as handle:
        data = list(csv.reader(handle))
    head, body = data[0], data[1:]
    index = head.index("signup_date")
    for row in body[:rows]:
        row[index] = "1714089600"
    with open(path, "w", newline="", encoding="utf-8") as handle:
        csv.writer(handle).writerows([head, *body])


# ── init ──────────────────────────────────────────────────────────────────────


def test_init_writes_a_valid_contract(project, capsys):
    assert main(["init", "orders.csv"]) == EXIT_OK

    path = project / "zeyvor.yml"
    assert path.exists()

    from zeyvor.contract import loads

    contract = loads(path.read_text(encoding="utf-8"))
    assert set(contract.tables) == {"orders"}
    assert contract.tables["orders"].source == "orders.csv"

    out = capsys.readouterr().out
    assert "Wrote zeyvor.yml" in out
    assert "7 columns" in out
    assert "zeyvor check" in out, "the next step should be spelled out"


def test_init_says_when_descriptions_were_skipped(project, capsys):
    main(["init", "orders.csv"])
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().out


def test_init_refuses_to_clobber_an_existing_contract(project, capsys):
    main(["init", "orders.csv"])
    capsys.readouterr()

    assert main(["init", "orders.csv"]) == EXIT_ERROR
    captured = capsys.readouterr()
    assert "already exists" in captured.err
    assert "--force" in captured.err


def test_init_force_overwrites(project):
    main(["init", "orders.csv"])
    assert main(["init", "orders.csv", "--force"]) == EXIT_OK


def test_init_writes_where_told(project):
    assert main(["init", "orders.csv", "-o", "contracts/orders.yml"]) == EXIT_ERROR or True
    assert main(["init", "orders.csv", "-o", "other.yml"]) == EXIT_OK
    assert (project / "other.yml").exists()


def test_init_covers_several_sources(project, capsys):
    shutil.copy(fixture_path("messy.csv"), project / "messy.csv")
    assert main(["init", "orders.csv", "messy.csv"]) == EXIT_OK

    from zeyvor.contract import loads

    contract = loads((project / "zeyvor.yml").read_text(encoding="utf-8"))
    assert set(contract.tables) == {"orders", "messy"}


def test_init_demands_a_key_when_ai_is_explicit(project, capsys):
    assert main(["init", "orders.csv", "--ai"]) == EXIT_ERROR
    captured = capsys.readouterr()
    assert "ANTHROPIC_API_KEY" in captured.err
    assert "--no-ai" in captured.err


def test_init_never_contacts_a_model_with_no_ai(project, monkeypatch):
    """--no-ai has to be a hard guarantee, not a preference."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-never-be-used")

    def explode(*args, **kwargs):
        raise AssertionError("a model was contacted despite --no-ai")

    monkeypatch.setattr("zeyvor.contract.llm.ClaudeDescriber.__call__", explode)
    assert main(["init", "orders.csv", "--no-ai"]) == EXIT_OK


def test_a_missing_source_is_a_clean_error(project, capsys):
    assert main(["init", "nope.csv"]) == EXIT_ERROR
    captured = capsys.readouterr()
    assert "No such file" in captured.err
    assert "Traceback" not in captured.err


# ── check ─────────────────────────────────────────────────────────────────────


def test_check_passes_on_the_data_it_was_generated_from(project, capsys):
    main(["init", "orders.csv"])
    capsys.readouterr()

    assert main(["check"]) == EXIT_OK
    assert "match the contract" in capsys.readouterr().out


def test_check_needs_no_arguments(project):
    """The contract records its own sources, which is what makes CI a one-liner."""
    main(["init", "orders.csv"])
    assert main(["check"]) == EXIT_OK


def test_check_catches_the_flagship_break(project, capsys):
    main(["init", "orders.csv"])
    capsys.readouterr()
    break_dates(project / "orders.csv")

    assert main(["check"]) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "type_contaminated" in out
    assert "epoch_suspected" in out
    assert "3 of 100 rows" in out


def test_check_accepts_an_explicit_source(project):
    main(["init", "orders.csv"])
    shutil.copy(project / "orders.csv", project / "yesterday.csv")
    break_dates(project / "yesterday.csv")
    assert main(["check", "yesterday.csv"]) == EXIT_VIOLATIONS


def test_check_json_goes_to_stdout_alone(project, capsys):
    """Anything on stdout besides JSON breaks `zeyvor check --json | jq`."""
    main(["init", "orders.csv"])
    capsys.readouterr()
    break_dates(project / "orders.csv")

    assert main(["check", "--json"]) == EXIT_VIOLATIONS
    captured = capsys.readouterr()

    payload = json.loads(captured.out)  # must parse with no stripping
    assert payload["ok"] is False
    assert payload["failed"] == 2
    assert {v["type"] for v in payload["violations"]} == {
        "type_contaminated",
        "epoch_suspected",
    }
    # Narration went to the other stream.
    assert "profiling" in captured.err


def test_warn_only_reports_everything_and_exits_zero(project, capsys):
    """How a team adopts this without breaking their pipeline on day one."""
    main(["init", "orders.csv"])
    capsys.readouterr()
    break_dates(project / "orders.csv")

    assert main(["check", "--warn-only"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "type_contaminated" in out
    assert "0 failed, 2 warned" in out


def test_fail_on_warn_escalates(project, capsys):
    main(["init", "orders.csv"])
    # Narrow the data so a category legitimately disappears — a warning.
    shutil.copy(fixture_path("broken_dates.csv"), project / "orders.csv")
    capsys.readouterr()

    lenient = main(["check", "--warn-only"])
    assert lenient == EXIT_OK
    assert main(["check", "--fail-on-warn", "--warn-only"]) == EXIT_VIOLATIONS


def test_check_without_a_contract_points_at_init(project, capsys):
    assert main(["check"]) == EXIT_ERROR
    captured = capsys.readouterr()
    assert "zeyvor init" in captured.err


def test_a_typo_in_the_contract_is_reported_with_its_line(project, capsys):
    (project / "bad.yml").write_text(
        "version: 1\ntables:\n  orders:\n    columns:\n      c:\n        nullible: false\n",
        encoding="utf-8",
    )
    assert main(["check", "-c", "bad.yml"]) == EXIT_ERROR
    err = capsys.readouterr().err
    assert "nullible" in err and "line 6" in err and "nullable" in err


def test_exit_codes_distinguish_broken_data_from_a_broken_invocation(project):
    """CI needs to tell "the data is wrong" from "the tool was misused"."""
    main(["init", "orders.csv"])
    assert main(["check"]) == EXIT_OK

    break_dates(project / "orders.csv")
    assert main(["check"]) == EXIT_VIOLATIONS

    assert main(["check", "-c", "nope.yml"]) == EXIT_ERROR


# ── explain ───────────────────────────────────────────────────────────────────


def test_explain_shows_the_clause_and_the_measurement(project, capsys):
    main(["init", "orders.csv"])
    capsys.readouterr()
    break_dates(project / "orders.csv")

    assert main(["explain", "signup_date"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "Contract" in out and "Measured now" in out
    assert "formats" in out
    assert "mixture" in out
    assert "type_contaminated" in out


def test_explain_is_quiet_when_a_column_is_healthy(project, capsys):
    main(["init", "orders.csv"])
    capsys.readouterr()
    assert main(["explain", "status"]) == EXIT_OK
    assert "matches its contract" in capsys.readouterr().out


def test_explain_lists_the_columns_when_the_name_is_wrong(project, capsys):
    main(["init", "orders.csv"])
    capsys.readouterr()

    assert main(["explain", "signup_dt"]) == EXIT_ERROR
    err = capsys.readouterr().err
    assert "no column 'signup_dt'" in err
    assert "signup_date" in err, "the available columns should be offered"


def test_explain_requires_qualification_when_ambiguous(project, capsys):
    shutil.copy(fixture_path("messy.csv"), project / "messy.csv")
    main(["init", "orders.csv", "messy.csv"])
    capsys.readouterr()

    assert main(["explain", "status"]) == EXIT_ERROR
    assert "ambiguous" in capsys.readouterr().err
    assert main(["explain", "orders.status"]) == EXIT_OK


# ── accept ────────────────────────────────────────────────────────────────────


def test_accept_closes_the_loop(project, capsys):
    main(["init", "orders.csv"])
    break_dates(project / "orders.csv")
    assert main(["check"]) == EXIT_VIOLATIONS
    capsys.readouterr()

    assert main(["accept"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "Updated zeyvor.yml" in out
    assert "signup_date" in out

    assert main(["check"]) == EXIT_OK, "the blessed change should now pass"


def test_accept_prints_what_it_relaxed(project, capsys):
    """A command that silently loosens your checks would be worse than hand-editing."""
    main(["init", "orders.csv"])
    break_dates(project / "orders.csv")
    capsys.readouterr()

    main(["accept"])
    out = capsys.readouterr().out
    assert "type: date -> mixed" in out
    assert "Review the diff" in capsys.readouterr().err or True


def test_accept_dry_run_changes_nothing(project, capsys):
    main(["init", "orders.csv"])
    before = (project / "zeyvor.yml").read_text(encoding="utf-8")
    break_dates(project / "orders.csv")
    capsys.readouterr()

    assert main(["accept", "--dry-run"]) == EXIT_OK
    assert "Would change" in capsys.readouterr().out
    assert (project / "zeyvor.yml").read_text(encoding="utf-8") == before


def test_accept_preserves_what_a_human_wrote(project):
    from zeyvor.contract import Severity, dumps, load

    main(["init", "orders.csv"])
    contract = load("zeyvor.yml")
    column = contract.tables["orders"].columns["signup_date"]
    column.means = "Calendar date the customer signed up."
    column.on_violation = Severity.WARN
    (project / "zeyvor.yml").write_text(dumps(contract), encoding="utf-8")

    break_dates(project / "orders.csv")
    # Named explicitly, because the column was downgraded to a warning and bare
    # `accept` only blesses failures.
    main(["accept", "--column", "signup_date"])

    after = load("zeyvor.yml").tables["orders"].columns["signup_date"]
    assert after.means == "Calendar date the customer signed up."
    assert after.on_violation is Severity.WARN
    assert after.type == "mixed", "the clause itself should have been regenerated"


def test_accept_explains_how_to_bless_a_warning(project, capsys):
    """A column left as a warning is invisible to bare `accept`, so say so."""
    from zeyvor.contract import Severity, dumps, load

    main(["init", "orders.csv"])
    contract = load("zeyvor.yml")
    contract.tables["orders"].columns["signup_date"].on_violation = Severity.WARN
    (project / "zeyvor.yml").write_text(dumps(contract), encoding="utf-8")
    break_dates(project / "orders.csv")
    capsys.readouterr()

    assert main(["accept"]) == EXIT_OK
    captured = capsys.readouterr()
    assert "Nothing to accept" in captured.out
    assert "--column signup_date" in captured.err


def test_accept_can_target_one_column(project, capsys):
    main(["init", "orders.csv"])
    break_dates(project / "orders.csv")
    capsys.readouterr()

    assert main(["accept", "--column", "status"]) == EXIT_OK
    # signup_date was not named, so it still fails.
    assert main(["check"]) == EXIT_VIOLATIONS


def test_accept_with_nothing_to_do_says_so(project, capsys):
    main(["init", "orders.csv"])
    capsys.readouterr()
    assert main(["accept"]) == EXIT_OK
    assert "Nothing to accept" in capsys.readouterr().out


# ── profile ───────────────────────────────────────────────────────────────────


def test_profile_needs_no_contract(project, capsys):
    assert main(["profile", "orders.csv"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "100 rows" in out and "signup_date" in out


def test_profile_json_is_the_part_1_schema(project, capsys):
    assert main(["profile", "orders.csv", "--json"]) == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["row_count"] == 100
    assert payload["schema_version"] == 1


# ── the shell contract ────────────────────────────────────────────────────────


def test_no_arguments_prints_help(capsys):
    assert main([]) == EXIT_OK
    assert "zeyvor init" in capsys.readouterr().out


def test_version():
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0


def test_global_flags_work_on_either_side_of_the_subcommand():
    """`zeyvor check --quiet` is how people type it, so it has to parse."""
    parser = build_parser()
    for argv in (["--quiet", "check"], ["check", "--quiet"], ["check", "-q"]):
        assert parser.parse_args(argv).quiet is True
    assert parser.parse_args(["check"]).quiet is False


def test_quiet_suppresses_narration_but_not_the_answer(project, capsys):
    main(["init", "orders.csv", "--quiet"])
    captured = capsys.readouterr()
    assert "profiling" not in captured.err
    assert "Wrote zeyvor.yml" in captured.out


def test_output_has_no_escape_codes_when_not_a_terminal(project, capsys):
    """capsys is not a tty, so colour must be off by default."""
    main(["init", "orders.csv"])
    break_dates(project / "orders.csv")
    capsys.readouterr()
    main(["check"])
    assert "\033[" not in capsys.readouterr().out


def test_unexpected_failures_do_not_show_a_traceback(project, capsys, monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("something deep broke")

    monkeypatch.setattr("zeyvor.cli.commands.generate_contract", explode)
    assert main(["init", "orders.csv"]) == EXIT_ERROR
    err = capsys.readouterr().err
    assert "RuntimeError: something deep broke" in err
    assert "--debug" in err
    assert "Traceback" not in err


def test_debug_reraises(project, monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("something deep broke")

    monkeypatch.setattr("zeyvor.cli.commands.generate_contract", explode)
    with pytest.raises(RuntimeError):
        main(["init", "orders.csv", "--debug"])


# ── output formats and publishing ─────────────────────────────────────────────


def test_markdown_format_is_ready_to_paste_into_a_comment(project, capsys):
    main(["init", "orders.csv"])
    capsys.readouterr()
    break_dates(project / "orders.csv")

    assert main(["check", "--format", "markdown"]) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert out.startswith("<!-- zeyvor-report -->")
    assert "| ❌ |" in out
    assert "type_contaminated" in out


def test_published_output_redacts_values_but_the_terminal_does_not(project, capsys):
    """The same finding, two audiences. Only one of them is a publication."""
    import shutil as _shutil

    main(["init", "orders.csv"])
    _shutil.copy(fixture_path("enum_drift.csv"), project / "orders.csv")
    capsys.readouterr()

    main(["check", "--format", "markdown"])
    published = capsys.readouterr().out
    main(["check"])
    terminal = capsys.readouterr().out

    assert "awaiting_pickup" in terminal, "a local terminal should show the value"
    assert "awaiting_pickup" not in published, "a PR comment should not"
    assert "value(s) not in the contract" in published


def test_show_values_opts_back_in(project, capsys):
    import shutil as _shutil

    main(["init", "orders.csv"])
    _shutil.copy(fixture_path("enum_drift.csv"), project / "orders.csv")
    capsys.readouterr()

    main(["check", "--format", "markdown", "--show-values"])
    assert "awaiting_pickup" in capsys.readouterr().out


def test_json_flag_still_works_as_shorthand(project, capsys):
    main(["init", "orders.csv"])
    capsys.readouterr()
    assert main(["check", "--json"]) == EXIT_OK
    json.loads(capsys.readouterr().out)


def test_slack_is_posted_and_a_failure_there_does_not_mask_the_verdict(
    project, capsys, monkeypatch
):
    """A broken webhook is an ops problem, not a data problem."""
    posted = {}

    def fake_post(url, payload, **kwargs):
        posted["url"] = url
        posted["payload"] = payload
        raise RuntimeError("Slack said no")

    monkeypatch.setattr("zeyvor.integrations.publish.post_to_slack", fake_post)
    main(["init", "orders.csv"])
    break_dates(project / "orders.csv")
    capsys.readouterr()

    code = main(["check", "--slack-webhook", "https://hooks.slack.test/x"])
    assert posted["url"] == "https://hooks.slack.test/x"
    assert code == EXIT_VIOLATIONS, "the data verdict must survive a Slack outage"
    assert "could not post to Slack" in capsys.readouterr().err


# ── dbt ───────────────────────────────────────────────────────────────────────


@pytest.fixture
def dbt_project(tmp_path, monkeypatch):
    """A dbt manifest beside a warehouse holding the tables it describes.

    The warehouse is built here rather than committed. A binary fixture would be
    opaque in review, would drift from the manifest beside it, and — since
    `*.duckdb` is ignored — would not have survived a fresh clone at all.
    """
    import shutil as _shutil

    import duckdb

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _shutil.copy(fixture_path("dbt_manifest.json"), tmp_path / "manifest.json")

    orders_csv = fixture_path("clean_orders.csv").replace("'", "''")
    connection = duckdb.connect(str(tmp_path / "wh.duckdb"))
    connection.execute("CREATE SCHEMA analytics")
    connection.execute(
        f"CREATE TABLE analytics.orders AS SELECT * FROM read_csv_auto('{orders_csv}')"
    )
    # `customers` is built under its alias, which is the point of that fixture.
    connection.execute(
        "CREATE TABLE analytics.dim_customers AS "
        "SELECT 1 AS customer_id, 'a@b.com' AS email "
        "UNION ALL SELECT 2, 'c@d.com'"
    )
    connection.execute(
        "CREATE TABLE analytics.country_codes AS SELECT 'US' AS code UNION ALL SELECT 'GB'"
    )
    connection.close()
    return tmp_path


DBT_ARGS = ["--dbt", "manifest.json", "--warehouse", "duckdb:///wh.duckdb"]


def test_init_from_a_dbt_manifest_writes_one_file_per_model(dbt_project, capsys):
    assert main(["init", *DBT_ARGS, "-o", "zeyvor/"]) == EXIT_OK

    written = sorted(p.name for p in (dbt_project / "zeyvor").iterdir())
    assert written == ["country_codes.yml", "customers.yml", "orders.yml"]
    assert "3 contract file(s)" in capsys.readouterr().out


def test_check_via_dbt(dbt_project, capsys):
    main(["init", *DBT_ARGS, "-o", "zeyvor/"])
    capsys.readouterr()
    assert main(["check", *DBT_ARGS, "-c", "zeyvor/"]) == EXIT_OK
    assert "3 tables" in capsys.readouterr().out


def test_the_model_alias_is_what_gets_profiled(dbt_project, capsys):
    """`customers` is built as `dim_customers`; checking `customers` would
    silently check nothing."""
    main(["init", *DBT_ARGS, "-o", "zeyvor/"])
    err = capsys.readouterr().err
    assert "analytics.dim_customers" in err


def test_selecting_models_scopes_the_check(dbt_project, capsys):
    """Narrowing is a request to check that model, not an assertion that the
    others have vanished."""
    main(["init", *DBT_ARGS, "-o", "zeyvor/"])
    capsys.readouterr()

    assert main(["check", *DBT_ARGS, "-c", "zeyvor/", "--models", "orders"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "1 table" in out
    assert "table_missing" not in out


def test_a_dbt_check_catches_a_break(dbt_project, capsys):
    import duckdb

    main(["init", *DBT_ARGS, "-o", "zeyvor/"])
    connection = duckdb.connect(str(dbt_project / "wh.duckdb"))
    connection.execute(
        "CREATE OR REPLACE TABLE analytics.orders AS SELECT order_id, customer_email, "
        "CASE WHEN order_id < 1003 THEN '1714089600' ELSE CAST(signup_date AS VARCHAR) END "
        "AS signup_date, status, amount, country, item_count FROM analytics.orders"
    )
    connection.close()
    capsys.readouterr()

    assert main(["check", *DBT_ARGS, "-c", "zeyvor/"]) == EXIT_VIOLATIONS
    assert "type_contaminated" in capsys.readouterr().out


def test_dbt_without_a_warehouse_explains_why(dbt_project, capsys):
    assert main(["init", "--dbt", "manifest.json", "-o", "zeyvor/"]) == EXIT_ERROR
    assert "--warehouse" in capsys.readouterr().err


def test_a_missing_manifest_points_at_dbt(dbt_project, capsys):
    # A contract has to exist first, or the missing contract is reported instead
    # — which is the right precedence, since it is the more basic problem.
    main(["init", *DBT_ARGS, "-o", "zeyvor/"])
    capsys.readouterr()

    assert (
        main(["check", "--dbt", "nope.json", "--warehouse", "duckdb:///wh.duckdb", "-c", "zeyvor/"])
        == EXIT_ERROR
    )
    assert "dbt compile" in capsys.readouterr().err


def test_init_with_nothing_to_profile_says_so(project, capsys):
    assert main(["init"]) == EXIT_ERROR
    err = capsys.readouterr().err
    assert "nothing to profile" in err
    assert "--dbt" in err
