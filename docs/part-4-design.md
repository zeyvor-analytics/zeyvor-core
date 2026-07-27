# Part 4 — Ship it, then automate it

Two halves, in this order. The first makes the tool obtainable and trustworthy;
the second makes it run without anyone remembering to. Doing them the other way
round would mean building an integration for a package nobody can install.

---

# Stage 1 — Blockers (about half a day)

## 4.1 Publish to PyPI

**Do this first, before anything else in Part 4.** Check the name is free. If
`zeyvor` is taken, that decision cascades into the Action name, the docs and the
install instructions, so it cannot wait.

- Verify availability on PyPI. Fallback names, in order of preference:
  `zeyvor-cli`, `zeyvorcheck`.
- Release **0.1.0** as the first public version, containing Parts 1–3. Nobody
  holds an earlier copy, so the history is ours to define.
- Dry run to TestPyPI first, install from it in a clean virtualenv, and confirm
  `zeyvor --version` and `zeyvor init` work from that install rather than from
  the source tree. This is also the moment the packaging is proven — the source
  layout and the `[project.scripts]` entry point have only ever been exercised
  through an editable install.
- Use **trusted publishing** (OIDC from GitHub Actions) rather than a long-lived
  API token in secrets.
- Sanity checks before the real upload: `python -m build`, `twine check dist/*`,
  and confirm the README renders on the project page.

Done when a stranger can run `pip install zeyvor && zeyvor --version`.

## 4.2 Our own CI

Distinct from the Action we *ship* — this is the repo's own hygiene, and
conflating the two is what let it go unscheduled.

- `.github/workflows/test.yml`: matrix of Linux, macOS, Windows × Python 3.10,
  3.11, 3.12, 3.13. Install, run the suite, report coverage.
- **Windows is the point.** The ASCII symbol fallback in `render.py` exists
  purely for a legacy Windows code page and has never executed. Add a unit test
  that forces a stream whose `encoding` is `cp1252` and asserts the ASCII set is
  chosen, so the behaviour is pinned even where the matrix does not exercise the
  console directly.
- Add `ruff` as a lint step with a light rule set. Not for style theatre — for
  catching unused imports and obvious mistakes across 5,000 lines.
- A separate `release.yml` triggered on a tag, doing the trusted-publish upload.

Done when a push runs 400+ tests on three operating systems and a tag publishes.

## 4.3 Verify the AI path for real

Every existing test mocks the model. The code is written and its failure paths
are handled, but the *quality of the output* is unknown, and it is the one part
of the product I cannot vouch for.

- Add `scripts/verify_ai.py` — outside the test suite, since it needs a key and
  a network. It runs `generate_contract` with a real `ClaudeDescriber` over three
  datasets: `clean_orders.csv` (obvious), `messy.csv` (ambiguous), and the real
  demo file (genuinely unclear columns).
- Judge the descriptions against the standard the prompt sets: one sentence, says
  what a value *represents*, names the unit, does not restate the type, admits
  ambiguity rather than inventing. Tune the prompt until they pass.
- Check the `unsafe` advice does its job: it should object to closing a category
  set that looks like it will grow, and stay quiet otherwise. A model that never
  objects is as useless as one that objects constantly.
- Record token usage per table so the cost of `init` is a known number.
- Commit one real generated contract into `docs/` as a worked example. Not
  asserted in tests — model output is not reproducible — but visible, so a
  reviewer can see what the tool actually produces.

Done when the descriptions are good enough to show a stranger, and the cost of
`init` is written down.

---

# Stage 2 — Automation

## 4.4 The GitHub Action

What a user writes:

```yaml
- uses: zeyvor/zeyvor-action@v1
  with:
    contract: zeyvor.yml
    warn-only: false
```

Decisions:

- **A composite action, not Docker.** It installs from PyPI and runs the CLI —
  seconds rather than a container pull, and no image to maintain.
- **Findings go to the step summary, not to annotations.** GitHub annotations
  need a file and a line number, and a data finding has neither. Writing rich
  markdown to `$GITHUB_STEP_SUMMARY` puts the report where a failing job already
  sends people.
- **One PR comment, edited in place.** A new comment per push turns a week of
  iteration into forty notifications. Find the previous comment by a hidden HTML
  marker and update it.
- Exit code passes through, so the job fails on `1` and errors on `2`.

### The privacy decision this forces

`--json` output can contain category values — business vocabulary like status
names, plan tiers, country codes. In `masked` mode that is intentional and
correct for a local terminal. **A pull-request comment on a public repository is
a different setting entirely.**

So: the Action's comment renderer reports types, counts, shares and shapes, and
**omits category values by default**, with an opt-in input for teams on private
repos who want the full detail. The CLI keeps its current behaviour; it is the
*publishing* step that tightens. Worth stating plainly in the docs, because
"your data never leaves your machine" has to stay true of the integration too.

## 4.5 dbt integration

The wedge audience runs dbt, so this is the piece that decides whether Part 4
produces users.

- Read `target/manifest.json` for models, and each model's database, schema and
  alias — that resolves a model to a real warehouse table.
- Handle several manifest schema versions. Read `metadata.dbt_schema_version` and
  be tolerant of field drift rather than pinning one dbt release.
- `zeyvor init --dbt target/manifest.json` generates contracts for every model;
  `zeyvor check --dbt target/manifest.json` verifies them after `dbt run`.
- `--models a b c` to narrow. Not dbt's full selection syntax — that is a rabbit
  hole, and a list covers the common case.

**This forces the multi-file contract layout deferred in Part 2.** A project with
fifty models cannot share one `zeyvor.yml`: every change would touch every
reviewer. So the loader gains directory support — `zeyvor/<model>.yml` — with the
single-file form still valid for the one-table case. Small change to
`schema.py`, but it is a prerequisite rather than a nice-to-have.

Warehouse credentials belong to the user's CI config. Document how to wire them,
and never log them.

## 4.6 Slack and scheduled runs

- `zeyvor check --slack-webhook $URL` posts a summary when something fails.
  Blocks payload, built by a pure function so it can be unit tested.
- Scheduled runs need no new code: cron in a workflow, plus the webhook.

## 4.7 Documentation

- README section on CI setup.
- `examples/` with a working GitHub Actions workflow and a dbt one.

---

## Testing plan

- **Pure functions get unit tests**: the JSON→markdown renderer, the PR comment
  payload builder, the Slack blocks builder, the dbt model→source resolver.
- **A committed `manifest.json` fixture** means the dbt path is tested without
  dbt installed, and without a warehouse.
- **Multi-file contract loading** gets loader tests alongside the existing
  single-file ones.
- **The Action itself gets one manual end-to-end** on a scratch repository. A
  composite action is mostly YAML; unit-testing it would test the mock.
- **`scripts/verify_ai.py` stays out of the suite** so the suite remains offline.

## Definition of done

`pip install zeyvor` works for a stranger. Tests run on push across three
operating systems. The AI descriptions have been read by a human and are good.
A dbt user can add ten lines to a workflow and have their models checked on every
push, with one tidy comment on the pull request and no data values leaked into
it.
