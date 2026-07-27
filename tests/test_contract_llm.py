"""The describer, with the model mocked.

The suite stays offline, so what is verified here is the contract *around* the
model: that the prompt carries statistics and never rows, that a malformed reply
cannot corrupt a contract, and above all that the model can only ever remove an
assertion — never add one.
"""

from __future__ import annotations

import json

import pytest

from zeyvor.contract import generate_contract
from zeyvor.contract.llm import (
    WITHDRAWABLE,
    ClaudeDescriber,
    apply_advice,
    build_prompt,
    parse_response,
)


class FakeBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class FakeMessages:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return type("Msg", (), {"content": [FakeBlock(self.reply)]})()


class FakeClient:
    def __init__(self, reply: str) -> None:
        self.messages = FakeMessages(reply)


# ── the prompt ────────────────────────────────────────────────────────────────


def test_the_prompt_carries_statistics_and_no_rows(profile_fixture):
    """The privacy claim has to hold at the one point data leaves the machine."""
    profile = profile_fixture("clean_orders.csv")
    contract = generate_contract(profile)
    prompt = build_prompt(profile, contract.tables["clean_orders"])

    payload = json.loads(prompt[prompt.index("{") :])
    assert payload["table"] == "clean_orders"
    columns = {c["name"]: c for c in payload["columns"]}
    assert columns["amount"]["measured_type"] == "float"
    assert "median" in columns["amount"]["numeric"]

    # No customer value appears anywhere in the prompt.
    assert "user0@example.com" not in prompt
    assert "@example.com" not in prompt


def test_low_cardinality_vocabulary_is_shared_because_a_contract_needs_it(profile_fixture):
    """Category members are business vocabulary and are needed to describe the
    column; they are the one value-level thing the default privacy mode shares."""
    profile = profile_fixture("clean_orders.csv")
    contract = generate_contract(profile)
    prompt = build_prompt(profile, contract.tables["clean_orders"])
    assert "shipped" in prompt


def test_the_prompt_states_which_clauses_are_proposed(profile_fixture):
    """The model can only object to a clause it can see."""
    profile = profile_fixture("clean_orders.csv")
    contract = generate_contract(profile)
    prompt = build_prompt(profile, contract.tables["clean_orders"])
    payload = json.loads(prompt[prompt.index("{") :])
    status = next(c for c in payload["columns"] if c["name"] == "status")
    assert "categories_closed" in status["proposed_clauses"]


# ── parsing ───────────────────────────────────────────────────────────────────


def test_a_well_formed_reply_parses():
    parsed = parse_response('{"columns": {"a": {"means": "First.", "unsafe": ["unique"]}}}')
    assert parsed["a"]["means"] == "First."
    assert parsed["a"]["unsafe"] == ["unique"]


def test_markdown_fences_and_chatter_are_tolerated():
    parsed = parse_response('Sure! ```json\n{"columns": {"a": {"means": "First."}}}\n```')
    assert parsed["a"]["means"] == "First."


def test_a_bare_string_value_is_accepted_as_the_description():
    parsed = parse_response('{"columns": {"a": "Just a sentence."}}')
    assert parsed["a"]["means"] == "Just a sentence."
    assert parsed["a"]["unsafe"] == []


def test_a_reply_with_no_json_is_rejected():
    with pytest.raises(ValueError, match="No JSON object"):
        parse_response("I am afraid I cannot help with that.")


def test_a_reply_without_a_columns_mapping_is_rejected():
    with pytest.raises(ValueError, match="no 'columns' mapping"):
        parse_response('{"tables": {}}')


# ── the asymmetry that matters ────────────────────────────────────────────────


def test_the_model_can_withdraw_an_assertion(profile_fixture):
    """A growing category set is exactly the judgement a model is good at."""
    profile = profile_fixture("clean_orders.csv")
    contract = generate_contract(profile)
    table = contract.tables["clean_orders"]
    assert table.columns["status"].categories_closed is True

    apply_advice(
        table,
        {"status": {"means": "Fulfilment state.", "unsafe": ["categories_closed"]}},
    )

    assert table.columns["status"].categories_closed is False
    assert table.columns["status"].categories is not None  # still documented


def test_the_model_cannot_add_an_assertion(profile_fixture):
    """The one hard boundary: a model that could add rules could invent a
    failure, and a checker that fails for invented reasons is worse than none."""
    profile = profile_fixture("clean_orders.csv")
    contract = generate_contract(profile)
    table = contract.tables["clean_orders"]

    before = {
        "unique": table.columns["amount"].unique,
        "formats": list(table.columns["amount"].formats),
        "max": table.columns["amount"].maximum,
    }

    # A reply asking for anything other than a withdrawal is ignored wholesale.
    apply_advice(
        table,
        {
            "amount": {
                "means": "Order total.",
                "unsafe": ["nullable", "type", "no_pii", "categories", "min_rows", "unique_key"],
            }
        },
    )

    assert table.columns["amount"].unique == before["unique"]
    assert table.columns["amount"].formats == before["formats"]
    assert table.columns["amount"].maximum == before["max"]
    assert table.columns["amount"].means == "Order total."


def test_only_a_known_clause_set_may_ever_be_withdrawn():
    assert {"categories_closed", "formats", "unique", "min", "max"} == WITHDRAWABLE


def test_advice_for_an_unknown_column_is_ignored(profile_fixture):
    profile = profile_fixture("clean_orders.csv")
    table = generate_contract(profile).tables["clean_orders"]
    apply_advice(table, {"no_such_column": {"means": "Nothing.", "unsafe": ["unique"]}})
    assert "no_such_column" not in table.columns


# ── the describer end to end ──────────────────────────────────────────────────


def test_the_describer_writes_descriptions_into_the_contract(profile_fixture):
    reply = json.dumps(
        {
            "columns": {
                "order_id": {"means": "Unique identifier for an order."},
                "amount": {"means": "Order total in USD.", "unsafe": ["max"]},
            }
        }
    )
    describer = ClaudeDescriber(client=FakeClient(reply), api_key="not-used")
    contract = generate_contract(profile_fixture("clean_orders.csv"), describer=describer)
    columns = contract.tables["clean_orders"].columns

    assert columns["order_id"].means == "Unique identifier for an order."
    assert columns["amount"].means == "Order total in USD."
    assert columns["amount"].maximum is None  # withdrawn on advice


def test_the_describer_sends_a_deterministic_request(profile_fixture):
    client = FakeClient('{"columns": {}}')
    describer = ClaudeDescriber(client=client, api_key="not-used")
    generate_contract(profile_fixture("clean_orders.csv"), describer=describer)

    call = client.messages.calls[0]
    assert call["temperature"] == 0, "descriptions must be reproducible"
    assert "data contract" in call["system"]


def test_a_garbled_reply_leaves_a_valid_contract(profile_fixture):
    """Generation must degrade to "undocumented", never to "broken"."""
    describer = ClaudeDescriber(client=FakeClient("not json at all"), api_key="not-used")
    contract = generate_contract(profile_fixture("clean_orders.csv"), describer=describer)
    columns = contract.tables["clean_orders"].columns

    assert columns["status"].categories_closed is True
    assert columns["status"].means is None
    assert "descriptions unavailable" in contract.generated_by


def test_a_missing_api_key_explains_that_checking_does_not_need_one(monkeypatch):
    pytest.importorskip("anthropic", reason="the key check only runs once the SDK is present")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    describer = ClaudeDescriber()
    with pytest.raises(RuntimeError) as excinfo:
        describer._ensure_client()
    message = str(excinfo.value)
    assert "ANTHROPIC_API_KEY" in message
    assert "check` never needs one" in message


def test_a_missing_sdk_names_the_extra_to_install(monkeypatch):
    """The dependency is optional, so the failure has to be self-explanatory."""
    import builtins

    real_import = builtins.__import__

    def refuse_anthropic(name, *args, **kwargs):
        if name == "anthropic":
            raise ImportError("No module named 'anthropic'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse_anthropic)
    with pytest.raises(RuntimeError) as excinfo:
        ClaudeDescriber(api_key="x")._ensure_client()
    assert "zeyvor[ai]" in str(excinfo.value)
