"""Recall, measured rather than asserted.

Every other test in this suite checks that a specific function behaves. This
one checks the claim the README actually makes — that breaking data in a
realistic way produces the finding a person needs — by breaking data in
twenty-odd realistic ways and counting.

It is slower than the rest of the suite because each case is a real `init` and
a real `check` in a subprocess. That is the point: an in-process shortcut would
share state between cases and measure something other than what a user gets.
"""

from __future__ import annotations

import pytest

from harness import run_mutation
from mutations import MUTATIONS

# Beyond this, one upstream change is producing a wall of findings and the
# cascade suppression in `diff.py` has stopped doing its job. Two is the
# observed ceiling across every mutation here; three leaves room for a
# legitimate new finding without letting a regression through unnoticed.
MAX_COLLATERAL = 3

# Each mutation is a pair of subprocesses, and both tests below need the same
# result. Running it twice would double the suite's slowest section to prove
# the same thing twice.
_CACHE: dict[str, object] = {}


def outcome_for(mutation):
    if mutation.name not in _CACHE:
        _CACHE[mutation.name] = run_mutation(mutation)
    return _CACHE[mutation.name]


@pytest.mark.parametrize("mutation", MUTATIONS, ids=lambda m: m.name)
def test_a_realistic_breakage_is_caught(mutation):
    """One known drift, injected on purpose, produces the finding it should.

    A failure here is a hole in the product rather than a broken assertion —
    unless the error says the mutation changed nothing, which means the test
    itself stopped testing.
    """
    outcome = outcome_for(mutation)
    assert not outcome.error, outcome.error
    assert outcome.caught, (
        f"{mutation.name}: expected one of {outcome.expected}, got {outcome.found or '()'}\n"
        f"  models: {mutation.models}"
    )


@pytest.mark.parametrize("mutation", MUTATIONS, ids=lambda m: m.name)
def test_one_cause_does_not_produce_a_wall_of_findings(mutation):
    """The number nobody was measuring.

    Recall is only half of it. A tool that catches everything and reports nine
    findings per cause is one people learn to skim, and skimming is how the
    real one gets missed. Cascade suppression exists to keep this near zero;
    nothing verified that it worked until now.
    """
    outcome = outcome_for(mutation)
    assert not outcome.error, outcome.error
    assert len(outcome.collateral) <= MAX_COLLATERAL, (
        f"{mutation.name} produced {len(outcome.collateral)} findings beyond the "
        f"expected ones: {outcome.collateral}"
    )
