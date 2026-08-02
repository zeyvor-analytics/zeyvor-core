"""The one place a language model is used: writing what each column means.

Scope is deliberately tiny. The model receives column *statistics* — never rows —
and returns one sentence per column, plus an optional list of assertions it
believes are unsafe to keep. It cannot add an assertion; only remove one. A model
that could add rules could invent a failure, and a checker that fails for
invented reasons is worse than no checker.

This runs once, at `zeyvor init`. `zeyvor check` never calls it, which is why
checking needs no API key, no network, and produces identical output every run.
"""

from __future__ import annotations

import json
import os
from typing import Any

from ..profile.models import TableProfile
from .models import TableContract

MODEL = "claude-opus-5"

# Thinking and the reply share this budget on current models, and a wide table
# asks for sixty descriptions in one JSON object. 4096 was sized for the reply
# alone and would now truncate mid-object.
MAX_TOKENS = 16000

SYSTEM_PROMPT = """\
You are documenting a data contract. For each column you are given measured \
statistics — never raw rows — and you write one short sentence explaining what \
the column holds and why it exists.

Return ONLY valid JSON of this exact shape:
{
  "columns": {
    "<column_name>": {
      "means": "One sentence, plain English, no restating of the type.",
      "unsafe": ["clause_name", ...]
    }
  }
}

Writing the description:
- One sentence. Say what the column is for, not what type it is — the type is \
already recorded next to your sentence.
- Name the unit where it matters ("Order total in USD", "Duration in seconds").
- Say what one value represents ("Calendar date the customer signed up").
- If the evidence is genuinely ambiguous, say so plainly rather than guessing.

The "unsafe" list is for assertions that look likely to cause false alarms, and \
it may only ever REMOVE a proposed clause. Valid entries: "categories_closed" \
(the category set looks like it will grow — new statuses, new countries, new \
plan names), "formats" (the format looks incidental rather than guaranteed), \
"unique" (uniqueness looks coincidental in this sample), "max" or "min" (the \
range looks likely to be exceeded by ordinary growth). Omit or leave empty when \
everything looks safe. Be conservative: only flag a clause when you have a \
concrete reason.
"""

# Only these clauses may be withdrawn on the model's advice.
WITHDRAWABLE = {"categories_closed", "formats", "unique", "min", "max"}


def _column_brief(column, contract_column) -> dict[str, Any]:
    """A compact, row-free description of one column for the prompt."""
    brief: dict[str, Any] = {
        "name": column.name,
        "measured_type": column.inferred_type.value,
        "declared_type": column.declared_type,
        "distinct_values": column.distinct_count,
        "rows": column.row_count,
        "null_rate": round(column.null_rate or 0.0, 4),
    }
    if column.dominant_shape:
        brief["dominant_shape"] = column.dominant_shape.shape
        brief["shape_coverage"] = column.shape_coverage
    if column.enum and column.enum.complete and not column.enum.hashed:
        brief["categories"] = column.enum.values()[:25]
    if column.numeric:
        brief["numeric"] = {
            "min": column.numeric.minimum,
            "max": column.numeric.maximum,
            "median": column.numeric.p50,
        }
    if column.temporal:
        brief["temporal"] = {
            "earliest": column.temporal.minimum,
            "latest": column.temporal.maximum,
        }
    if column.observations:
        brief["findings"] = column.observations
    if column.pii_signals:
        brief["pii_signals"] = column.pii_signals
    # What we intend to assert, so the model can object to specific clauses.
    proposed = [
        name
        for name, present in (
            ("unique", contract_column.unique),
            ("formats", bool(contract_column.formats)),
            ("categories_closed", contract_column.categories_closed),
            ("min", contract_column.minimum is not None),
            ("max", contract_column.maximum is not None),
        )
        if present
    ]
    if proposed:
        brief["proposed_clauses"] = proposed
    return brief


def build_prompt(profile: TableProfile, table: TableContract) -> str:
    payload = {
        "table": profile.name,
        "row_count": profile.row_count,
        "columns": [
            _column_brief(column, table.columns[column.name])
            for column in profile.columns
            if column.name in table.columns
        ],
    }
    return "Document this table. Return only the JSON.\n\n" + json.dumps(
        payload, indent=2, ensure_ascii=False
    )


def response_text(message: Any) -> str:
    """The written reply, from a response that may hold more than one block.

    Never ``content[0]``. Models think by default now, so the first block is
    routinely a thinking block — and because thinking text is withheld unless
    asked for, that block stringifies to something that merely *looks* like an
    unparseable reply. The failure would read as "the model wrote nonsense"
    rather than "we read the wrong block".
    """
    parts = [
        block.text
        for block in getattr(message, "content", []) or []
        if getattr(block, "type", "") == "text"
    ]
    if not parts:
        raise ValueError(
            "The response carried no text block "
            f"(blocks: {[getattr(b, 'type', '?') for b in getattr(message, 'content', []) or []]})"
        )
    return "\n".join(parts)


def parse_response(raw: str) -> dict[str, dict[str, Any]]:
    """Extract the columns mapping from a model response, strictly."""
    text = raw.strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    first, last = text.find("{"), text.rfind("}")
    if first == -1 or last == -1:
        raise ValueError("No JSON object found in the response")
    parsed = json.loads(text[first : last + 1])
    columns = parsed.get("columns")
    if not isinstance(columns, dict):
        raise ValueError("Response contained no 'columns' mapping")
    out: dict[str, dict[str, Any]] = {}
    for name, body in columns.items():
        if isinstance(body, str):
            out[str(name)] = {"means": body, "unsafe": []}
        elif isinstance(body, dict):
            unsafe = body.get("unsafe") or []
            out[str(name)] = {
                "means": str(body.get("means", "")).strip(),
                "unsafe": [str(u) for u in unsafe if isinstance(unsafe, list)],
            }
    return out


def apply_advice(table: TableContract, advice: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Write descriptions and withdraw clauses the model judged unsafe.

    Applies everything it is given — descriptions and withdrawals both — and
    returns the descriptions so the caller can satisfy the ``describer``
    protocol. Only removal of a clause is ever permitted; see the module
    docstring for why that boundary is absolute.
    """
    descriptions: dict[str, str] = {}
    for name, body in advice.items():
        column = table.columns.get(name)
        if column is None:
            continue
        if body.get("means"):
            descriptions[name] = body["means"]
            column.means = body["means"]
        for clause in body.get("unsafe", []):
            if clause not in WITHDRAWABLE:
                continue
            if clause == "categories_closed":
                column.categories_closed = False
            elif clause == "formats":
                column.formats = []
            elif clause == "unique":
                column.unique = None
            elif clause == "min":
                column.minimum = None
            elif clause == "max":
                column.maximum = None
    return descriptions


class ClaudeDescriber:
    """A `describer` for ``generate_contract``.

    >>> contract = generate_contract(profile, describer=ClaudeDescriber())

    Constructing it does not require a key; calling it does. Failure is
    non-fatal by design — an undocumented contract is still a working contract.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = MODEL,
        client: Any = None,
    ) -> None:
        self.model = model
        self._client = client
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import anthropic  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError(
                "Writing descriptions needs the Anthropic SDK: "
                "pip install 'zeyvor[ai]'  (or generate without descriptions)"
            ) from exc
        if not self._api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Generate without descriptions, or "
                "export the key. Note that `zeyvor check` never needs one."
            )
        self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    def __call__(self, profile: TableProfile, table: TableContract) -> dict[str, str]:
        client = self._ensure_client()
        # No `temperature`. Current models reject sampling parameters outright,
        # and the `temperature=0` this used to send never actually guaranteed
        # identical output — it bought a reproducibility that was not real.
        # What makes `check` reproducible is that it never calls a model.
        message = client.messages.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_prompt(profile, table)}],
        )
        return apply_advice(table, parse_response(response_text(message)))


__all__ = [
    "MODEL",
    "SYSTEM_PROMPT",
    "WITHDRAWABLE",
    "ClaudeDescriber",
    "build_prompt",
    "response_text",
    "parse_response",
    "apply_advice",
]
