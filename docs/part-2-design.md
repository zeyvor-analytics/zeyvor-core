# Part 2 — Contract & Diff Engine

Design settled before implementation. Part 1 measures; Part 2 decides whether
what was measured is acceptable. This is the part that produces findings a user
acts on, and the part where the product either earns trust or loses it.

## The one risk that matters

A checker that raises false alarms gets uninstalled within a week. Every design
decision below is subordinate to that, which produces one governing rule:

> **Only assert what the profile actually supports.**

No closed category set unless the profile recorded a *complete* one. No format
rule unless shape coverage was near-total. No `nullable: false` unless the
baseline had zero nulls. A generated contract must pass against the data it was
generated from — there is a test for exactly that, and it is the most important
test in Part 2.

## Architecture decision: the LLM runs once, at init — never at check time

Contract *generation* uses Claude to write what each column means and to propose
which assertions are sensible. Contract *checking* is entirely deterministic:
templated messages, no network, no API key.

This matters more than it first appears:

- `zeyvor check` runs on every push. An LLM call per run would be slow, costly,
  and non-deterministic — the same data could pass on Monday and fail on Tuesday.
- **No API key is needed in CI.** Nobody has to put an Anthropic secret into
  their GitHub Actions config to adopt the tool. That removes the single biggest
  approval hurdle for a team trying it.
- Violation messages are reproducible, so they can be asserted in tests.

`init` also gets a `--no-ai` mode that emits the same contract with descriptions
left blank, for users who will not send even column statistics anywhere.

## The file format

YAML, because the audience already reads dbt YAML. One file may describe several
tables; the CLI in Part 3 decides file layout (a single `zeyvor.yml`, or
`zeyvor/<table>.yml` per table for cleaner PR diffs).

```yaml
version: 1
generated_by: zeyvor 0.2.0
generated_at: 2026-05-18

defaults:
  on_violation: fail          # fail | warn | ignore

tables:
  orders:
    source: orders.csv
    profile_fingerprint: sha256:14e1ad482a98f6f4   # what this was born from
    min_rows: 1
    allow_new_columns: true   # a new column is news, not a failure
    allow_missing_columns: false

    columns:
      order_id:
        means: Unique identifier for an order.
        type: integer
        unique: true
        nullable: false

      signup_date:
        means: Calendar date the customer signed up.
        type: date
        formats: ["####-##-##"]
        nullable: false
        min: 2019-01-01
        max: today            # 'today' and 'now' are resolved at check time

      status:
        means: Fulfilment state of the order.
        type: text
        categories: [pending, shipped, delivered, refunded]
        categories_closed: true

      amount:
        means: Order total in USD.
        type: float
        min: 0
        max: 10000
        max_null_rate: 0.01
        on_violation: warn    # column-level override of defaults

      notes:
        means: Free-text notes written by support agents.
        type: text
        no_pii: true          # PII appearing here is a violation

      internal_scratch:
        ignore: true          # documented as deliberately unchecked
```

Format notes:

- **`formats` uses shapes, not strftime.** `####-##-##` is what the profiler
  measures and, for a non-programmer reviewing a PR, is arguably clearer than
  `%Y-%m-%d`. A list, because two legitimate formats can coexist.
- **Explicit threshold keys** (`max_null_rate: 0.01`) rather than expression
  strings (`null_rate: "<0.01"`). Less magic, no expression parser, better error
  messages.
- **Tolerances exist everywhere they need to.** `nullable: false` is absolute;
  `max_null_rate` is the graded version. A contract with no graded options
  breaks on the first stray null and teaches users to disable it.
- **No baseline statistics stored.** Storing last run's p50/max would make every
  contract churn on every run. `min`/`max` are a user-approved envelope, not a
  snapshot — so contracts stay stable and diffs stay meaningful.
- **`ignore: true`** documents intent. Columns simply absent from the contract
  are unchecked (subject to `allow_new_columns`).

## Violation taxonomy

Each maps a profile fact against a contract clause. Default severity in
brackets; all overridable.

| Violation | Trigger | Sev |
|---|---|---|
| `column_missing` | contracted column absent from the profile | fail |
| `column_added` | new column, when `allow_new_columns: false` | fail |
| `type_changed` | inferred type differs from the contracted type | fail |
| `type_contaminated` | contracted type still dominant, but a minority type appeared | fail |
| `format_changed` | dominant shape is not in `formats` | fail |
| `new_category` | a value outside a closed category set | fail |
| `category_disappeared` | a contracted category is no longer present | warn |
| `nullability_violated` | nulls present where `nullable: false` | fail |
| `null_rate_exceeded` | null rate above `max_null_rate` | fail |
| `uniqueness_lost` | duplicates in a column contracted `unique` | fail |
| `range_exceeded` | numeric or temporal value outside `min`/`max` | fail |
| `pii_appeared` | PII detected where `no_pii: true` | fail |
| `row_count_below_min` | volume collapse | fail |
| `epoch_suspected` | Part 1 observation, on a temporal column | fail |
| `excel_serial_suspected` | Part 1 observation, on a temporal column | fail |
| `mixed_boolean_encoding` | Part 1 observation | warn |
| `null_words_appeared` | text standing in for missing data | warn |
| `mojibake_appeared` | encoding damage | warn |
| `unit_shift_suspected` | values within type but outside the approved envelope | fail |

`type_contaminated` is the flagship. A date column at 99.8% dates and 0.2%
integers has *not* changed type — a naive type equality check passes it — and it
is precisely the case Zeyvor exists for. It is a distinct violation from
`type_changed` because the message and the remedy differ.

## Message templates

Deterministic, evidence-interpolated, and written to be read by someone who did
not write the pipeline:

```
❌ orders.signup_date — type_contaminated
   Contract: calendar dates (####-##-##)
   Found:    99.8% dates, 0.2% integers (1,000 of 500,000 rows)
   The integers look like Unix timestamps (e.g. shape ##########).
   Likely an upstream format change. Fix the source, or widen the contract:
       formats: ["####-##-##", "##########"]
```

Three parts, always: what was promised, what was found, what to do. The
suggested remediation is generated from the diff, not from a model.

## Module layout

```
src/zeyvor/contract/
  __init__.py
  models.py       Contract, TableContract, ColumnContract, Severity
  schema.py       YAML load/dump + validation with line-numbered errors
  generate.py     profile -> contract (deterministic scaffold; optional LLM prose)
  diff.py         (profile, contract) -> Report
  violations.py   Violation model, taxonomy, message templates
  llm.py          Anthropic client, prompt, strict JSON parsing, offline fallback
```

New dependency: `PyYAML` (hard). `anthropic` becomes an optional extra —
checking must work without it installed.

## What generation asserts, mechanically

Deterministic scaffolding, straight from the profile:

| Contract clause | Emitted when |
|---|---|
| `type` | always, from `inferred_type` |
| `unique: true` | `is_unique` |
| `nullable: false` | `null_count == 0` |
| `max_null_rate` | nulls present — set to observed rate, rounded up with headroom |
| `formats` | `shape_coverage >= 0.99` and `shape_distinct_count <= 3` |
| `categories` + `categories_closed` | `enum.complete` and not unique |
| `min`/`max` | numeric or temporal, widened past observed extremes |
| `no_pii: true` | no PII observed **and** the column is not obviously a PII column |

The LLM adds only `means`, and may *downgrade* a proposed assertion it thinks is
unsafe (e.g. "this looks like a growing category set, don't close it"). It can
never introduce an assertion the profile doesn't support — that stays a
deterministic decision so a model cannot invent a false alarm.

Range widening needs care: a `max` set exactly at the observed maximum fails on
the next larger order. Plan: pad numeric ranges (round to a significant figure
above the observed max) and leave temporal `max: today` where the data is
historic. Exact padding policy to be tuned against the fixtures.

## Test plan

Beyond unit coverage of each violation:

1. **No false alarms.** Contract generated from `clean_orders.csv`, checked
   against `clean_orders.csv` → zero violations. Non-negotiable.
2. **The golden path.** Contract from `clean_orders.csv`, checked against
   `broken_dates.csv` → `type_contaminated` + `epoch_suspected`, asserted on
   exact message text.
3. **Enum drift.** Contract from `clean_orders.csv` vs `enum_drift.csv` →
   `new_category: awaiting_pickup`.
4. **Unit shift.** Contract with a padded range vs `unit_shift.csv` →
   `range_exceeded`, and *not* `type_changed`.
5. **Round trip.** contract → YAML → contract preserves everything.
6. **Malformed YAML** produces an error naming the line.
7. **Severity → exit code** mapping.
8. **LLM is mocked**; the suite stays offline. A generation test with `llm=None`
   must still produce a valid, useful contract.

New fixtures needed: a column that legitimately gains a category (to prove
`categories_closed: false` stays quiet), and a nullable column crossing its
`max_null_rate`.

## Deliberately out of scope for Part 2

- The CLI (`zeyvor init` / `check` / `explain`) — Part 3.
- Cross-table relationship contracts (foreign keys). The inference exists in
  the old TypeScript code and is worth porting, but referential integrity needs
  a join across sources and belongs after single-table checking is solid.
- Contract migration/versioning tooling (`zeyvor accept` to bless a change).
  Likely a Part 3 command, noted here so the format leaves room for it.

## Definition of done

`generate_contract(profile, llm=None) -> Contract`, `Contract.to_yaml/from_yaml`,
`check(profile, contract) -> Report` with typed violations, severities, an exit
code and rendered English messages — plus the eight tests above, all offline.
