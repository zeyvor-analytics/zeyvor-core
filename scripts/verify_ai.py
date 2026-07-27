#!/usr/bin/env python
"""Exercise the one code path the test suite cannot: a real model call.

    export ANTHROPIC_API_KEY=...
    python scripts/verify_ai.py

Everything in `tests/` mocks the model, which proves the plumbing and proves
nothing about the writing. This script generates contracts for three datasets of
increasing difficulty, prints the descriptions for a human to judge, and reports
what the model asked to withdraw and what it cost.

Deliberately outside the test suite: it needs a key and a network, and the suite
must stay runnable offline.

What to look for, in order of importance:

1. **Does the description say what a value represents?** "Calendar date the
   customer signed up" is useful. "A date column" is not — the type is already
   recorded on the line above it.
2. **Are units named?** "Order total in USD" beats "the order total". A missing
   unit is how a dollars-to-cents change goes unnoticed by a human reviewer.
3. **Does it admit ambiguity?** For a column called `code` holding five-digit
   numbers, "purpose unclear from the data" is the honest answer and a better
   one than a confident guess.
4. **Does the withdrawal advice discriminate?** It should object to closing a
   category set that will obviously grow, and stay quiet on a stable one. A
   model that never objects is as useless as one that objects to everything.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from zeyvor import profile_source  # noqa: E402
from zeyvor.contract import dumps, generate_contract  # noqa: E402
from zeyvor.contract.llm import ClaudeDescriber  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(os.path.dirname(HERE), "tests", "fixtures")

CASES = [
    ("clean_orders.csv", "obvious — names say what the columns are"),
    ("messy.csv", "ambiguous — defects, disguised numbers, PII in free text"),
    ("wide.csv", "opaque — col_000..col_059, nothing to go on but the values"),
]


class CountingDescriber(ClaudeDescriber):
    """Wraps the describer to record tokens and latency."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.input_tokens = 0
        self.output_tokens = 0
        self.seconds = 0.0

    def __call__(self, profile, table):
        client = self._ensure_client()
        started = time.perf_counter()
        from zeyvor.contract.llm import (
            MAX_TOKENS,
            SYSTEM_PROMPT,
            apply_advice,
            build_prompt,
            parse_response,
        )

        message = client.messages.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            temperature=0,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_prompt(profile, table)}],
        )
        self.seconds += time.perf_counter() - started
        usage = getattr(message, "usage", None)
        if usage:
            self.input_tokens += getattr(usage, "input_tokens", 0)
            self.output_tokens += getattr(usage, "output_tokens", 0)

        block = message.content[0]
        raw = block.text if getattr(block, "type", "") == "text" else str(block)
        advice = parse_response(raw)
        self.last_advice = advice
        return apply_advice(table, advice)


def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set.", file=sys.stderr)
        print("This script exists to test the one path the suite cannot.", file=sys.stderr)
        return 2

    total_in = total_out = 0
    total_seconds = 0.0

    for filename, difficulty in CASES:
        path = os.path.join(FIXTURES, filename)
        print("=" * 78)
        print(f"{filename}  —  {difficulty}")
        print("=" * 78)

        profile = profile_source(path)
        describer = CountingDescriber()
        contract = generate_contract(profile, describer=describer)
        table = contract.tables[profile.name]

        for name, column in table.columns.items():
            means = column.means or "\033[31m(no description)\033[0m"
            print(f"\n  {name}")
            print(f"    {means}")
            clauses = []
            if column.type:
                clauses.append(f"type={column.type}")
            if column.categories_closed:
                clauses.append(f"categories closed ({len(column.categories or [])})")
            if column.formats:
                clauses.append(f"formats={column.formats}")
            if column.unique:
                clauses.append("unique")
            if clauses:
                print(f"    \033[2m{'  '.join(clauses)}\033[0m")

        withdrawn = {
            name: body.get("unsafe", [])
            for name, body in getattr(describer, "last_advice", {}).items()
            if body.get("unsafe")
        }
        print("\n  ── withdrawals ──")
        print(f"  {withdrawn or 'none — check that this is right, not just quiet'}")

        total_in += describer.input_tokens
        total_out += describer.output_tokens
        total_seconds += describer.seconds
        print(
            f"\n  {describer.input_tokens:,} in / {describer.output_tokens:,} out tokens"
            f" · {describer.seconds:.1f}s\n"
        )

        if filename == "clean_orders.csv":
            out = os.path.join(os.path.dirname(HERE), "docs", "example-contract.yml")
            with open(out, "w", encoding="utf-8") as handle:
                handle.write(dumps(contract))
            print(f"  wrote {out} as a worked example\n")

    # Sonnet 4 list pricing at the time of writing: $3/Mtok in, $15/Mtok out.
    cost = total_in / 1e6 * 3 + total_out / 1e6 * 15
    print("=" * 78)
    print(
        f"{len(CASES)} tables · {total_in:,} in / {total_out:,} out tokens · "
        f"{total_seconds:.1f}s · about ${cost:.3f}"
    )
    print("`init` is a once-per-table cost. `check` never calls a model at all.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
