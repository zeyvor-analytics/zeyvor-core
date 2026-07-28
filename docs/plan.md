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

**Part 6 — Hosted dashboard.** Report ingest, history per column, trends, and a
read-only share link. Mostly a re-scope of the existing Next.js app.

*Done, with two amendments.* **Billing is cut** — Zeyvor is free permanently, so
this part no longer has a revenue rationale and has to justify itself on
usefulness alone. **Teams is deferred** — invites, roles and row-level security
are real complexity solving a problem nobody has at zero users. The medallion
pipeline builder was removed here rather than re-scoped: it was a different
product sharing a login.

**Part 7 — Cross-table checks.** Foreign keys and referential integrity: catching
that orders point at customers who no longer exist. The inference already exists
in the old TypeScript (`inferRelationships.ts`) and is worth porting. Scheduled
here on purpose — real users on single-table checking should shape what
cross-table checking looks like, rather than us guessing twice.

*Done, and the schedule did not hold: there are still no real users, so the
defaults were chosen on judgment. The port also dropped the original's LLM pass —
a relationship is an assertion that fails builds, and a model may only remove
assertions here — which exposed that the deterministic rule missed the ordinary
star schema. A second rule closes it.*

**Part 8 — Full audit and cleanup.** Every file in both repos: dead code,
awkward seams, consistency, and the performance items. Last, so we are not
tidying things still in flux.

*The performance guess here was wrong, which is the argument for measuring.* The
regex aggregates cost almost nothing — removing all 29 per column saved 3% of a
profile. The real cost was that three of the four measurement passes emitted one
`FROM <source>` **per column**, so a batch of twenty parsed the same CSV twenty
times: 43 of the 50 seconds a 200-column profile took. Naming the source in a
CTE fixed it — 50s to 9s, and 28% less memory, because twenty concurrent CSV
parses cost more than one scan.

Memory turned out to be governed by `--batch-size`, not by anything exotic: a
50-column, 200k-row file needs ~1.5GB at a batch of 20 and 540MB at a batch of 5.
That flag was hidden behind `argparse.SUPPRESS`, which made the only real
mitigation for an out-of-memory error undiscoverable, and DuckDB's own OOM advice
pointed at settings this package owns and the user cannot reach.
