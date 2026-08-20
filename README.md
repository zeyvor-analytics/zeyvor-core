# Zeyvor

**Your tests check that the boxes are filled in. Zeyvor checks that what's in the box still matches the label on it.**

A column called `signup_date` has held values like `2024-03-11` for two years. Then someone changes an upstream API, and next Tuesday it starts arriving as `1714089600`.

Nothing breaks. No error, no alert. The column is still complete, still unique, still the expected row count. Your dbt tests pass and your pipeline runs green — while every dashboard filtered by date is now silently wrong, and you find out six weeks later when someone in finance says the numbers look weird.

```
❌ signup_date — contract says calendar dates, found 10-digit integers (3% of rows).
                 These look like Unix timestamps. Upstream format likely changed.
```

---

## Status

**v0.7.0 — feature-complete, lightly field-tested.** The profiler, the contract engine, the CLI, CI integrations, cross-table (foreign key) checks, freshness and volume trends, cross-column rules, the web presence and the hosted dashboard are all built and covered by tests. What hasn't happened yet is much real data from people other than the author: the thresholds that decide what counts as drift worth failing a build over were tuned on judgment, not on a range of unfamiliar datasets, and that's the one thing that can't be fixed by writing more code.

If you run it and a finding looks wrong — too sensitive, not sensitive enough, or just mistaken — [tell us](mailto:zeyvor.analytics@gmail.com). That feedback is the actual gap right now.

## Install

```bash
pip install zeyvor
```

DuckDB and PyYAML are the only required dependencies. Warehouse drivers are optional extras:

```bash
pip install 'zeyvor[bigquery]'
```

## Use

```bash
zeyvor init orders.csv     # write a contract describing the data as it is now
zeyvor check               # verify live data against it — this is your CI step
```

That is the whole loop. `init` measures the data and writes `zeyvor.yml`, which you read, correct and commit. `check` needs no arguments, because the contract records what it describes.

When something breaks:

```
✖ orders.signup_date — type_contaminated
    Contract: calendar dates ('####-##-##')
    Found:    97.0% date, 3.0% integer — 3 of 100 rows (3.0%) do not fit
    Shapes present: ####-##-## (97), ########## (3)
    → Fix the source. If the new values are legitimate, widen the contract.

✖ orders.signup_date — epoch_suspected
    Found:    3 of 100 rows (3.0%) look like Unix timestamps
    This did not happen when the contract was written, and the rows that do
    parse are as wrong as the rows that do not.
    → Convert at the source, or widen the contract if intended.

2 failed, 0 warned across 7 columns
```

Exit code `1`. Your build is red, on the day it broke, naming the column.

### The five commands

| | |
|---|---|
| `zeyvor init <source>...` | Write a contract from current data |
| `zeyvor check [source]...` | Verify data against the contract |
| `zeyvor explain <column>` | What a column promises, beside what it does |
| `zeyvor accept` | Bless an intentional change |
| `zeyvor profile <source>` | Just look at the data, no contract involved |

Useful flags: `--json` for machine-readable output on stdout, `--warn-only` to report everything and still exit 0 (how a team adopts this without breaking their pipeline on day one), `--fail-on-warn` to go the other way, `--privacy strict` to let nothing recognisable leave the machine.

Exit codes are part of the interface: `0` matched, `1` the data violated the contract, `2` the invocation failed. CI needs to tell a broken contract file from broken data.

## In CI

```yaml
- uses: actions/checkout@v4
- uses: zeyvor-analytics/zeyvor-core@v1
    with:
      contract: zeyvor.yml
```

That is the whole setup. **No API key** — checking never calls a model. Findings land in the job summary and in a single pull-request comment that is edited in place rather than reposted on every push.

While adopting, `warn-only: true` reports everything and never fails the build.

### dbt

dbt already knows which tables exist and where. Point Zeyvor at the manifest and supply the connection once:

```bash
zeyvor init  --dbt target/manifest.json --warehouse "bigquery://project" -o zeyvor/
zeyvor check --dbt target/manifest.json --warehouse "bigquery://project" -c zeyvor/
```

Seeds and snapshots are included; ephemeral models are skipped, since they are inlined as CTEs and have no table to check. A model's **alias** is used rather than its name — checking the wrong table would be a silent no-op. `--models orders customers` narrows, and narrowing scopes the check rather than reporting everything else as missing.

With many models, `-o zeyvor/` writes one file per model, so a change to one model touches one file in review.

Working examples for both are in [`examples/`](examples/).

### What gets published

A pull-request comment is a publication; your terminal is not. So published output **omits category values by default** — status names, plan tiers and region codes stay out of a comment on a public repo — and reports types, counts, shares and shapes instead. `--show-values` (or `show-values: true`) opts back in for private repositories.

```
❌  `signup_date`  `type_contaminated`  97.0% date, 3.0% integer — 3 of 100 rows do not fit
```

Slack works the same way: `zeyvor check --slack-webhook $URL`.

### It remembers, locally

`zeyvor check` used to answer one question — does the data match right now — and
then forget, which left a whole class of failure invisible. A table that
normally loads fifty thousand rows and today loaded thirty thousand has violated
nothing: `min_rows` is satisfied, every column is the right type, nothing is
null that should not be. Only the comparison against previous runs says anything
is wrong.

So each check appends a few counts to `.zeyvor/history/`, and compares against
them next time:

```
! daily — volume_drop
    Contract: about 49,900 rows, the median of the last 6 runs
    Found:    28,000 rows — 44% below normal
    Every row here may be valid. What changed is how many arrived, which no
    single run can show.
```

That table's `min_rows` is 20,000. Twenty-eight thousand clears it comfortably,
having lost two fifths of the day.

**It warns rather than fails, by default.** A trend is fuzzier than a fixed
bound — the Monday after a holiday is genuinely quiet — and a check that reds a
build on an ordinary slow week teaches people to stop reading it. Set
`volume_tolerance: 0.25` on a table to promote it to a failure.

**Counts, never values.** Row count, the structural fingerprint, and per column
the number of nulls and distinct values. Enough for volume trends and null-rate
drift; deliberately not enough to reconstruct a row. A hundred runs is about
twenty-four kilobytes.

**It ignores itself.** A `.gitignore` is written alongside, because committing
it would put a small change in every pull request forever. Delete that file if
you would rather carry history in the repository itself. `--no-history` turns
the whole thing off.

**It needs a machine that persists.** A cron box or a long-lived worker
accumulates history naturally. A fresh CI runner does not: the directory is
built and destroyed inside one job, so the count never passes the three runs a
trend needs and `volume_drop` can never fire. The GitHub Action caches the
directory between runs to fix this, but a cache is evicted after a week of
disuse and is scoped per branch, so treat local trends as best-effort. A missing
baseline means no trend reported — never a wrong one. For a baseline that is
genuinely durable, `--upload` keeps it server-side.

### Keeping a history on the web (optional)

Everything above works with no account, no API key and no network. Zeyvor is a
local tool and stays one.

What the local history cannot do is outlive the machine it is on, or gather
several projects into one view. A dashboard can — which is what an account is
for, and the only thing it is for.

It is opt-in per run, and off unless you ask:

```bash
zeyvor check --upload --project acme/warehouse
```

Create a free project at [zeyvor.com/dashboard](https://zeyvor.com/dashboard),
then set `ZEYVOR_TOKEN` to its token — a project token, not your password, scoped
to one project and revocable.

**What is sent is deliberately narrow.** A finding's *type*, the table and column
names, a severity, and numbers. No values — not redacted, not truncated, not
collected. There is no `--show-values` equivalent here, unlike a pull-request
comment, because a comment is read by people you chose and a server is a third
party holding a copy. Column names *are* sent, because a history keyed on
anonymous ids would be unreadable — and since a name can itself be sensitive
(`patients_hiv_positive`), that is the reason this is opt-in rather than on.

A failed upload never changes the exit code. A reporting service being
unreachable is not a data problem, so it must not turn a green build red, nor
hide a real failure.

Point it at almost anything:

```bash
zeyvor init orders.csv                                # local file
zeyvor init "data/*.parquet"                          # glob
zeyvor init https://host/export.csv                   # remote file
zeyvor init "postgres://user:pw@host/db#public.orders" # live table
zeyvor init "bigquery://project#dataset.orders"        # warehouse
```

**Freshness — the failure with no evidence.** `max` bounds the future; `fresh_within`
bounds the past, and they catch opposite things. A table whose loader stopped on
Tuesday violates nothing else in a contract: every row is well formed, correctly
typed, complete, and inside every stated range. It has simply stopped growing,
and no other clause asks *when* the newest row arrived.

```yaml
loaded_at:
  type: timestamp
  fresh_within: 24h    # the newest value must be under a day old
```

`init` writes this only when the column *name* says it records when a row was
written — `updated_at`, `loaded_at`, `ingested_at` — **and** the data already
looks live. A `birth_date` is decades old and perfectly healthy; asserting it
should refresh nightly would fail a build every night on data behaving exactly
as intended, which is how a check gets deleted rather than fixed.

**A whole schema at once.** A `*` in the table part asks the database what it
holds, because naming two hundred tables on a command line is not a workflow
anybody sustains. System schemas are never profiled, so `#*` means your tables,
not Postgres's two hundred catalog relations.

```bash
zeyvor init "postgres://user:pw@host/db#public.*"    -o contracts/  # one schema
zeyvor init "postgres://user:pw@host/db#*"           -o contracts/  # every schema
zeyvor init "postgres://user:pw@host/db#public.stg_*" -o contracts/ # one layer
```

`-o contracts/` writes one file per table, so a change to one model touches one
file and a pull request stays readable. `zeyvor check -c contracts/` reads the
directory back, and the same wildcard works there — a contract generated from a
wildcard has to be checkable by one.

**A committed contract should never hold a credential.** `${VAR}` inside a database
source is expanded from the environment at the moment a connection is made, and
only there — the contract records the literal, unexpanded string. Single quotes
matter here: double quotes let the shell expand `${DB_PASSWORD}` itself before
Zeyvor ever sees the argument, which puts the real password on the command line
and in shell history — exactly what this is for avoiding.

```bash
export DB_PASSWORD=...
zeyvor init 'postgres://user:${DB_PASSWORD}@host/db#public.orders'
```

writes `source: postgres://user:${DB_PASSWORD}@host/db#public.orders` to
`zeyvor.yml` — safe to commit, safe to review in a diff. A rotated password is
then a changed environment variable, not a changed file.

Or use it as a library — the CLI is a thin shell over it:

```python
from zeyvor import profile_source
from zeyvor.contract import check, generate_contract, loads

report = check(profile_source("orders.csv"), loads(open("zeyvor.yml").read()))
print(report.render())
raise SystemExit(report.exit_code)
```

## Contracts

The generated file is meant to be read and edited in a pull request, so every
column carries a plain-English line saying what it currently promises, and the
file opens with a guide to reading the clauses:

```yaml
tables:
  orders:
    min_rows: 50000
    columns:
      # Dates, never empty, between 2019-01-01 and today, shaped like '####-##-##'.
      signup_date:
        means: Calendar date the customer signed up.
        type: date
        formats: ['####-##-##']
        nullable: false
        min: '2019-01-01'
        max: today
      # Text, and only these 4 values.
      status:
        type: text
        categories: [delivered, pending, refunded, shipped]
        categories_closed: true
      # Text.
      notes:
        type: text
        no_pii: true
        known_issues: [mojibake]

relationships:
  - means: Every order belongs to a customer.
    from: orders.customer_id
    to: customers.id
    cardinality: many_to_one
```

**`zeyvor check` needs no API key.** A language model is used exactly once, at generation time, to write the `means` lines — and it may only ever *remove* an assertion it judges unsafe, never add one. Checking is templated and deterministic, so it is free, instant, identical between runs, and needs no secret in CI.

**A generated contract always passes against the data it came from.** Every clause comes from measured evidence, and clauses that cannot be established are simply omitted: no closed category set unless the profile captured a complete one, no format rule on numbers (a digit count grows), no range on an identifier (an auto-incrementing id outgrows every ceiling), no uniqueness unless the column looks like a key. Pre-existing defects are recorded as `known_issues` rather than raised as news. There is a test for this on every fixture, and it is the most important test in the suite.

**Tolerances everywhere.** `nullable: false` has `max_null_rate` beside it; `defaults: {on_violation: warn}` turns the whole contract into a report so a team can adopt it without breaking their pipeline on day one; `ignore: true` retires a check while keeping the intent visible in review.

**Rules compare columns to each other.** Everything above looks at one column on its own, so nothing catches a row that shipped before it was ordered: both dates are real, neither is null, each sits inside its own range. A `rules:` block on a table says what has to hold *between* columns of the same row.

```yaml
    rules:
      - shipped_at >= ordered_at
      - abs(total - (subtotal - discount)) <= 0.01
      - status = 'shipped' implies shipped_at is not null
```

Comparisons, arithmetic, `and`/`or`/`not`, `is null`, `implies`, `abs()` and `length()` — a grammar Zeyvor parses and compiles per engine, not SQL pasted into a query. That keeps the contract a description rather than an executable artifact, and keeps a contract written against Postgres working on BigQuery. Every rule on a table is measured in one query.

A rule you wrote **fails** by default, unlike the generated thresholds, because it is a statement of intent rather than a guess; `max_violation_rate: 0.001` tolerates a known trickle. **A null makes a rule unknown, not broken** — say `is not null` when you mean to assert about nulls, or nulls would be reported by every rule that happens to touch the column, on top of `nullable`.

`init` never writes a rule that is switched on. It suggests a few, commented out, and only ones that held for every row it measured — a rule nobody agreed to is how a team learns to distrust the whole file.

**Relationships are checked across tables.** Give `init` more than one source and it proposes foreign keys from column names and uniqueness — deterministically, with no model involved, because a relationship is an assertion that fails builds and the model is only ever allowed to remove assertions here. `check` then measures each one with a single pushed-down anti-join: orphan rows, distinct missing keys, and whether the parent's key is still unique enough for the join not to fan out. `max_orphan_rate` exists for the soft-deleted dimension every real warehouse has somewhere.

Twenty-seven violation types, each with a default severity. `type_contaminated` is deliberately separate from `type_changed`: a column at 99.8% dates has *not* changed type, so equality checks pass it, and it is the case this exists to catch. Cascade suppression keeps one problem from producing five findings — a changed type silences the format, range and category clauses that follow from it.

## How it works

**Nothing is downloaded.** Every number in a profile is a SQL aggregate executed where the data already lives — DuckDB locally for files, the warehouse itself for BigQuery. A 200-column table costs the same handful of queries as a 5-column one, because all per-column metrics are computed as expressions inside a single `SELECT`.

```
pass 1   row count
pass 2   every scalar metric for every column          (batched)
pass 3   value-shape histograms                        (one query per batch)
pass 4   category sets for low-cardinality columns     (one query per batch)
```

**Types are measured, not trusted.** Files are read as all-text on purpose, so a bad value can never break profiling, and the type of each column is established from cast probes and format evidence. The type the source *claims* is recorded separately — and a disagreement between the two is itself a finding.

**Shapes carry the evidence.** Each value is reduced to a signature: digits to `#`, letters to `a`. `2024-03-11` becomes `####-##-##`; `1714089600` becomes `##########`. Grouping by signature reveals a format change without revealing a single value.

Measured on a 51 MB / 500,000-row / 12-column CSV: **5.3s in 6 queries**, and the 1,000 contaminated rows (0.2% of the table) were found. Inside a memory-capped CI container, pass `memory_limit` so the engine spills to disk rather than being killed:

```python
profile_source("orders.csv", memory_limit="1GB", threads=2)
```

## Privacy

The output is designed to be safe to commit to git, paste into a pull request, and send to a language model.

- No row is ever fetched. Every figure is an aggregate.
- Minimum and maximum *values* of text columns are never collected — only lengths. Alphabetical extremes are real customer data, so the profiler never asks for them.
- Columns where every value is distinct are never recorded as category sets, so a profile can't become a dump of customer names.

Three modes, with `masked` the default:

| Mode | Category values | Sample values |
|---|---|---|
| `strict` | hashed | none |
| `masked` *(default)* | kept — they're business vocabulary | none |
| `full` | kept | up to 5 per column |

Turning privacy up costs nothing in accuracy — `strict` and `masked` produce identical findings, and there's a test that fails if that ever stops being true.

## What it catches

Every case below is a real production failure that passes conventional checks. Each one is a test in [`tests/test_semantic_cases.py`](tests/test_semantic_cases.py).

| Finding | The failure |
|---|---|
| `epoch_suspected` | A date column starts receiving Unix timestamps |
| `excel_serial_suspected` | Dates became `45231` via a spreadsheet round-trip |
| `mixed_types` | Two upstream systems, two conventions, one column |
| `multiple_date_formats` | `11/03` is March 11th to one system and November 3rd to another |
| `pii_in_free_text` | Support agents pasting emails into a `notes` column |
| `leading_zeros` | `00123` → `123`, and joins fail on a subset of rows |
| `currency_in_text` | `SUM(revenue)` returns zero for a quarter |
| `numeric_stored_as_text` | The column is numeric; the type is not |
| `mixed_boolean_encoding` | A flag spelled `true`/`TRUE`/`yes`/`1`/`t` |
| `null_words` | `N/A` and `-` are missing data that no null check counts |
| `mojibake` | An encoding step is broken |
| `whitespace_padding` | `' Alice'` and `'Alice'` are two customers to a `GROUP BY` |
| `declared_type_conflict` | The schema and the data disagree outright |
| `enum_candidate` | The category set a contract will be written against |
| `fk_orphans` | Child rows point at parents that are no longer there |
| `fk_fanout` | A parent key gained duplicates, so every join through it multiplies rows |
| `relationship_uncheckable` | A join cannot be measured, so a green build is not evidence |
| `stale_data` | The loader stopped: every row still valid, nothing new arriving |
| `volume_drop` | Row count well below its recent normal — a load that half-worked |
| `rule_violated` | Two columns stopped agreeing: shipped before ordered, a total that no longer adds up |

Precision is treated as seriously as recall. Five-digit numbers are *not* reported as postal codes, `11.03.2024` is *not* reported as a phone number, and a date column sprinkled with `N/A` is *not* reported for inconsistent capitalisation — because a checker that cries wolf gets uninstalled in a week.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
```

733 tests, no network access required. Regenerate fixtures with `python tests/fixtures/generate.py`.

Patterns are tested by executing them inside DuckDB rather than Python's `re`, which verifies both correctness and RE2 compatibility — the property that lets the same expression run on BigQuery too.

### Recall, measured rather than asserted

Every threshold in `contract/generate.py` carries a comment justifying it, and
until recently every one of those numbers was chosen by reasoning about what
ought to work. `tests/mutations.py` replaces the reasoning with a measurement.

It takes known-clean data, breaks it in one specific realistic way — an epoch
swap, dollars becoming cents, two percent type contamination, a loader that
stopped, two columns that stopped agreeing — and names the finding that should
result. `tests/harness.py` then runs the real `init` → `check` sequence against
each one, in a subprocess, and records what came back.

```bash
PYTHONPATH=tests:src python tests/harness.py
```

Current result: **23 of 23 breakages caught, exercising 21 of the 27 finding
types, with no single cause producing more than two findings beyond the
expected one.**

That last number matters as much as the first. A checker that catches
everything and reports nine findings per cause is one people learn to skim, and
skimming is how the real finding gets missed. Cascade suppression exists to
keep it near zero; nothing verified that it worked until this harness existed.

### False positives, measured the same way

Recall is half the question. The failure people actually abandon a tool over is
being told a hundred things are wrong when nothing is, so
[`tests/corpus/`](tests/corpus/) runs `init` and `check` across ninety real
public datasets that nobody tampered with.

```bash
python tests/corpus/runner.py
```

| | |
|---|---|
| contract checked against the data it was generated from | **89/89 clean** |
| contract on the first half, checked against the second | 55/89 flagged |
| the same split, shuffled first | 29/89 flagged — **32.6%** |

The third row is the honest false-positive figure. The second is higher because
a sequential split is a split in time for most datasets, so much of what it
flags is real change the tool is supposed to report; shuffling removes that, and
twenty-six of the fifty-five fall silent.

The first row was **83/89 on the first run**. Those six failures were three real
bugs — a negative ceiling padded downward, a decimal column contracted as whole
numbers, and type contamination ignoring `known_issues` — none of which any
fixture could have caught, because no fixture had a negative number in it.

Two honest limits. Six finding types are not exercised yet — the three
foreign-key checks, `table_missing`, `volume_drop` and
`categories_unverifiable` — because they need more than one table or more than
one run, and the harness currently does neither. And this measures *recall*
only: whether real, unbroken data produces findings it should not is a separate
question, needing a corpus of datasets nobody tampered with.

<details>
<summary>Troubleshooting: <code>pip list</code> shows zeyvor but <code>import zeyvor</code> fails (macOS)</summary>

macOS sometimes sets the `UF_HIDDEN` flag on the `.pth` file pip writes for an editable install, and Python 3.11+ silently ignores hidden `.pth` files. Clear it:

```bash
chflags nohidden .venv/lib/python3.*/site-packages/_editable_impl_zeyvor.pth
```

Running the test suite is unaffected, since pytest is configured with `pythonpath = ["src"]`.
</details>

## Layout

```
src/zeyvor/
  engines/          where SQL runs: DuckDB, BigQuery + dialects
  profile/          Part 1 — measurement
    models.py       the profile data model (the interface to everything downstream)
    sql.py          SQL generation — every measurement as an aggregate
    types.py        inference and findings, derived from counts alone
    patterns.py     the pattern library
    privacy.py      what may leave the machine
    profiler.py     orchestration
  contract/         Part 2 — judgement
    models.py       Contract / TableContract / ColumnContract
    schema.py       zeyvor.yml read and write, with line-numbered errors
    generate.py     profile -> contract; asserts only what evidence supports
    diff.py         profile x contract -> violations (deterministic, offline)
    violations.py   the taxonomy and how findings read
    llm.py          the one place a model is used: writing `means`
  cli/              Part 3 — the command line
    main.py         argument parsing, dispatch, exit codes
    commands.py     init / check / explain / accept / profile
    render.py       terminal output: colour, symbols, width
  integrations/     Part 4 — other people's tools
    dbt.py          manifest -> tables, read defensively across dbt versions
    publish.py      markdown and Slack, with values redacted by default
  sources.py        source string → engine + relation
```

## License

MIT
