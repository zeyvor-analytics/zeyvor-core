# The corpus run

Phase 1 measured recall — break something known, check it is caught. This
measures the opposite and more dangerous failure: being told a hundred things
are wrong when nothing is. A checker that cries wolf gets uninstalled in a
week, and until this existed nobody had any idea how often Zeyvor did.

Ninety real datasets from `vega/vega-datasets` (BSD-3) and `plotly/datasets`
(MIT). The manifest is committed; the data is not. Redistributing other
people's datasets would mean auditing ninety licences and carrying fifteen
megabytes forever, so `build_manifest.py` enumerates them through the GitHub
API and `runner.py` caches downloads into a gitignored directory.

```bash
python tests/corpus/build_manifest.py     # refresh the list of datasets
python tests/corpus/runner.py             # run all ninety
python tests/corpus/runner.py 10          # or just the first ten
```

Not part of the test suite and not in CI. It needs network, takes several
minutes, and a dataset host having a bad afternoon should not turn somebody's
build red.

## Three stages

**Stage A — the invariant.** `init` a dataset, then `check` the same untouched
bytes. `generate.py` opens by calling this the governing rule: a contract must
pass against the data it was generated from. Any finding is a bug, not a
judgement call.

**Stage B — sequential halves.** Contract on the first half of the file, check
against the second. For most real datasets this is a split in time.

**Stage C — shuffled halves.** The same split, but the rows are shuffled first
against a fixed seed. Both halves now come from one distribution, so nothing
found here is real drift.

Stage C exists because Stage B on its own cannot be read. A finding in a
sequential split may be genuine change over the file — a price that rose, a
category introduced in March — which the tool is *supposed* to report.
Shuffling removes that, so the gap between B and C is the part of B that was
the tool doing its job.

## Results

Ninety datasets, 89 usable, one too narrow to split.

| | |
|---|---|
| **Stage A** — contract against its own data | **89/89 clean** |
| **Stage B** — sequential halves | 55/89 flagged (61.8%) |
| **Stage C** — shuffled halves | 29/89 flagged (**32.6%**) |

**32.6% is the honest false-positive figure.** Twenty-six of the fifty-five
datasets flagged in Stage B fall silent once shuffled, meaning those were real
changes across the file and correctly reported.

Stage A was **83/89 on the first run**. The six failures were three real bugs,
all fixed, none of which any fixture could have caught because no fixture
contained a negative number or a slightly dirty column:

- a negative ceiling padded *downward*, so every longitude, temperature or
  depth column got a contract it immediately failed
- a column containing decimals contracted as whole numbers, because an
  `integer` clause accepts only integers while `float` accepts both
- type contamination ignoring `known_issues`, so a mixture the contract had
  already accepted was re-reported on every run forever

## What the remaining 32.6% is

Not evenly spread. In Stage C the complaints are led by `range_exceeded` (16),
`category_disappeared` (16), `new_category` (10) and `type_contaminated` (10).

Several of those look like sampling rather than error. A category appearing
three times in a thousand rows will land entirely in one half often enough, and
`category_disappeared` firing on that is arithmetically correct and practically
useless. That is a threshold worth tuning rather than a bug worth fixing — and
tuning it trades against recall, which the Phase 1 harness can now measure. The
two together are what make that a decision with evidence behind it rather than
a guess.
