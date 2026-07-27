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

**Part 1 of 6 is complete: the profiler.** This is the measurement engine — point it at data and get back a structured, privacy-safe description of what the values actually are.

The contract format, the diff engine, the CLI, the CI integrations and the hosted dashboard are still to come. What's here works and is tested, but the public command surface (`zeyvor init`, `zeyvor check`) does not exist yet.

## Install

```bash
pip install zeyvor
```

DuckDB is the only required dependency. Warehouse drivers are optional extras:

```bash
pip install 'zeyvor[snowflake]'    # or [bigquery]
```

## Use

```python
from zeyvor import profile_source

profile = profile_source("orders.csv")

profile.column("signup_date").inferred_type      # InferredType.DATE
profile.column("notes").observations             # ['pii_in_free_text', 'mojibake']
profile.column("status").enum.values()           # ['shipped', 'pending', 'refunded']
print(profile.to_json())
```

Point it at almost anything:

```python
profile_source("orders.csv")                              # local file
profile_source("data/*.parquet")                          # glob
profile_source("https://host/export.csv")                 # remote file
profile_source("postgres://user:pw@host/db#public.orders") # live table
profile_source("snowflake://ACCOUNT#DB.SCHEMA.ORDERS")     # warehouse
```

Or look at a profile by eye while developing:

```bash
python -m zeyvor orders.csv
```

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

Precision is treated as seriously as recall. Five-digit numbers are *not* reported as postal codes, `11.03.2024` is *not* reported as a phone number, and a date column sprinkled with `N/A` is *not* reported for inconsistent capitalisation — because a checker that cries wolf gets uninstalled in a week.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
```

221 tests, no network access required. Regenerate fixtures with `python tests/fixtures/generate.py`.

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
  profile/
    models.py       the profile data model (the interface to everything downstream)
    sql.py          SQL generation — every measurement as an aggregate
    types.py        inference and findings, derived from counts alone
    patterns.py     the pattern library
    privacy.py      what may leave the machine
    profiler.py     orchestration
  sources.py        source string → engine + relation
```

## License

MIT
