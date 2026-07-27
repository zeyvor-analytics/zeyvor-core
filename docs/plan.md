# The conversion plan, revised

Parts 1–3 are done. This revision folds in five gaps that the original six-part
plan did not schedule, and adds one part that was deferred and never slotted.

## What changed and why

| Gap | Where it went |
|---|---|
| Cannot `pip install zeyvor` | **Part 4, first** — everything downstream builds on something nobody can obtain |
| Tests never run on push | **Part 4, first** — a matrix on Linux/macOS/Windows |
| Windows never tested | **Part 4** — free once the matrix exists |
| AI descriptions never actually generated | **Part 4, first** — one real run, then tune the prompt |
| No cross-table (foreign key) checks | **Part 7** — new, deliberately after real users exist |
| High memory use, loose ends | **Part 8** — the audit, explicitly named rather than implied |

Two problems cannot be fixed by building anything: tuned defaults and real-user
validation. Both need strangers running this on data we have never seen, which
is what Parts 4 and 5 exist to produce.

## The parts

**Part 4 — Ship it, then automate it.** Publish to PyPI, run our own tests on
every push across three operating systems, verify the AI path for real. Then the
GitHub Action, the dbt integration, and Slack alerts. Detail in
[part-4-design.md](part-4-design.md).

**Part 5 — Web presence.** Rewrite the landing page around one promise. Build
the free in-browser demo (DuckDB-WASM, file never leaves the machine). Cut the
retired features from the public site. Docs. *Loose end to clear first: the
`zeyvor-landing` work is committed but unpushed on a machine-named branch.*

**Part 6 — Hosted dashboard.** Report ingest, history per column, trends, shared
contracts, teams, alert routing, billing. Mostly a re-scope of the existing
Next.js app.

**Part 7 — Cross-table checks.** Foreign keys and referential integrity: catching
that orders point at customers who no longer exist. The inference already exists
in the old TypeScript (`inferRelationships.ts`) and is worth porting. Scheduled
here on purpose — real users on single-table checking should shape what
cross-table checking looks like, rather than us guessing twice.

**Part 8 — Full audit and cleanup.** Every file in both repos: dead code,
awkward seams, consistency, and the performance items (memory use, the cost of
23 regex aggregates per column). Last, so we are not tidying things still in
flux.
