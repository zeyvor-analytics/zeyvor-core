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

**v0.1 — feature-complete, not yet field-tested.** The profiler, the contract engine, the CLI, CI integrations, cross-table (foreign key) checks, the web presence, and the hosted dashboard are all built and covered by tests. What hasn't happened yet is real data from people other than the author: the thresholds that decide what counts as drift worth failing a build over were tuned on judgment, not on a range of unfamiliar datasets, and that's the one thing that can't be fixed by writing more code.

If you run it and a finding looks wrong — too sensitive, not sensitive enough, or just mistaken — [tell us](mailto:zeyvor.analytics@gmail.com). That feedback is the actual gap right now.

## Install

```bash
pip install zeyvor
```

DuckDB and PyYAML are the only required dependencies. Warehouse drivers are optional extras:

```bash
pip install 'zeyvor[snowflake]'    # or [bigquery]
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
zeyvor init  --dbt target/manifest.json --warehouse "snowflake://ACCOUNT" -o zeyvor/
zeyvor check --dbt target/manifest.json --warehouse "snowflake://ACCOUNT" -c zeyvor/
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

Point it at almost anything:

```bash
zeyvor init orders.csv                                # local file
zeyvor init "data/*.parquet"                          # glob
zeyvor init https://host/export.csv                   # remote file
zeyvor init "postgres://user:pw@host/db#public.orders" # live table
zeyvor init "snowflake://ACCOUNT#DB.SCHEMA.ORDERS"     # warehouse
```

Or use it as a library — the CLI is a thin shell over it:

```python
from zeyvor import profile_source
from zeyvor.contract import check, generate_contract, loads

report = check(profile_source("orders.csv"), loads(open("zeyvor.yml").read()))
print(report.render())
raise SystemExit(report.exit_code)
```

## Contracts

The generated file is meant to be read and edited in a pull request:

```yaml
tables:
  orders:
    columns:
      signup_date:
        means: Calendar date the customer signed up.
        type: date
        formats: ['####-##-##']
        nullable: false
        min: '2019-01-01'
        max: today
      status:
        type: text
        categories: [delivered, pending, refunded, shipped]
        categories_closed: true
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

**Relationships are checked across tables.** Give `init` more than one source and it proposes foreign keys from column names and uniqueness — deterministically, with no model involved, because a relationship is an assertion that fails builds and the model is only ever allowed to remove assertions here. `check` then measures each one with a single pushed-down anti-join: orphan rows, distinct missing keys, and whether the parent's key is still unique enough for the join not to fan out. `max_orphan_rate` exists for the soft-deleted dimension every real warehouse has somewhere.

Twenty-four violation types, each with a default severity. `type_contaminated` is deliberately separate from `type_changed`: a column at 99.8% dates has *not* changed type, so equality checks pass it, and it is the case this exists to catch. Cascade suppression keeps one problem from producing five findings — a changed type silences the format, range and category clauses that follow from it.

## How it works

**Nothing is downloaded.** Every number in a profile is a SQL aggregate executed where the data already lives — DuckDB locally for files, the warehouse itself for Snowflake and BigQuery. A 200-column table costs the same handful of queries as a 5-column one, because all per-column metrics are computed as expressions inside a single `SELECT`.

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

Precision is treated as seriously as recall. Five-digit numbers are *not* reported as postal codes, `11.03.2024` is *not* reported as a phone number, and a date column sprinkled with `N/A` is *not* reported for inconsistent capitalisation — because a checker that cries wolf gets uninstalled in a week.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
```

471 tests, no network access required. Regenerate fixtures with `python tests/fixtures/generate.py`.

Patterns are tested by executing them inside DuckDB rather than Python's `re`, which verifies both correctness and RE2 compatibility — the property that lets the same expression run on BigQuery and Snowflake.

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
  engines/          where SQL runs: DuckDB, Snowflake, BigQuery + dialects
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
