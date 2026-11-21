# The TCKDB pin and drift tracking

This repository is tested against one TCKDB commit. This page says where that pin lives,
how the scheduled workflow moves it, and what a person must still do when it moves.

## Where the pin lives

| Place | Holds |
|---|---|
| `tckdb-pin.toml` | The TCKDB repository URL, the commit `sha`, and the subdirectories of the two packages. CI reads it at run time, so no workflow file contains a commit. |
| `pyproject.toml` of `tckdb_core`, `tckdb_arc`, `tckdb_chemkin` and `tckdb_qcschema` | The `tckdb-schemas` and `tckdb-client` bounds, each one minor release line (`>=X.Y,<X.(Y+1)`). A package states only the ones it depends on: `tckdb-chemkin` has no `tckdb-schemas` bound. |
| `tckdb_core/tckdb_core/testing/contract.py` | `TARGET_SCHEMAS_LINE`, the `tckdb-schemas` line the test suites target. It exists once. The core's and the ARC adapter's `test_contract_pin.py` fail when the installed `tckdb-schemas` or their own `pyproject.toml` bound disagrees with it. `tckdb-chemkin` and `tckdb-qcschema` do not depend on the core, so their `tests/test_contract_pin.py` check the bounds each one states (`tckdb-client`, and for `tckdb-qcschema` `tckdb-schemas`) against the installed packages. `tools/tests/test_pin_lines.py` checks that every package states the core's lines, `tckdb-client` bounds included. |
| `README.md`, `adapters/tckdb_arc/README.md` | The install lines and the sentences that name the tested versions. |

The packages `--bump` moves are the rows of the `PACKAGES` table in `tools/tckdb_drift.py`.
A row is a `Package` with these fields (its docstring says the same):

| Field | Holds |
|---|---|
| `directory`, `module` | The package's directory and its import name; every path the tool touches comes from them. |
| `dist` | The distribution name in its `pyproject.toml`. |
| `version_in_init` | Whether `<module>/__init__.py` holds a `__version__` that must equal the `pyproject.toml` version. |
| `readme_sentence` | Whether the package's `README.md` has the "`tckdb-client` A.B.x and `tckdb-schemas` X.Y.x" sentence. |
| `changelog_style` | `marker` (one line under `<!-- tckdb-drift:changelog -->`) or `sections` (a `## X.Y.Z` section, newest first). |
| `note` | What a `sections` entry says about the package itself. |

The drift workflow and the publish script ask the tool for the directories and files
(`--list-packages`, `--bumped-files`) instead of repeating them, and the drift tests build their
synthetic repository and their list of directories from the table, so adding a package is adding
its row and nothing else in `tools/` or the drift tests. (The real package needs the files its
row names: a `CHANGELOG.md` with the marker or version headings, the `__version__` line, the
README sentence.) `tools/python_release.py` has its own table, of what is published to PyPI; a
test checks that the two name the same directories. Another test fails when any `pyproject.toml`
in the repository that depends on `tckdb-client` or `tckdb-schemas` is not in the drift table, or
a row of the table has no package on disk.

## Moving the pin is a reading task

The tests validate each payload against the JSON Schema of the installed `tckdb-schemas`.
A green suite after a bump shows that the payloads the tests build still validate. It does
not show that a new required field, convention or refusal code is handled correctly.
Before a bump is merged, read what changed:

```bash
python -m tckdb_schemas.contract --since <the previous X.Y.0>
python -m tckdb_schemas.contract --print
```

For every changed route, field or refusal code, decide whether the adapter handles it or
is unaffected. If a newly required convention is one the source does not state, the
adapter refuses to build that block; it does not fill a default to make the suite pass.

## The scheduled workflow

`.github/workflows/tckdb-drift.yml` runs daily and on demand.

Its `test` job has a read-only token. It compares the pinned release lines with the `main`
branch of the TCKDB repository named in `tckdb-pin.toml`. On a newer line it:

1. bumps the pins in the runner (a package behind the others is repaired; one ahead of the target makes the bump refuse, below);
2. installs both TCKDB packages from the new commit and every package of this repository;
3. fetches TCKDB's `backend/app/chemistry` at that new commit (`tools/fetch_tckdb_backend.sh`,
   the script `ci.yml` uses at the old one) and exports `TCKDB_BACKEND_PATH` and
   `TCKDB_REQUIRE_BACKEND=1` to the suites, so the comparisons with TCKDB's own code (the
   level-of-theory hash, the energy-level declaration, the method families) run, and fail rather
   than skip when the backend cannot be imported. One comparison still skips, in the drift job
   and in `ci.yml` alike: `test_hash_matches_the_real_backend_function` imports TCKDB's own
   `_level_of_theory_hash`, which needs the backend's web dependencies (fastapi, sqlalchemy)
   that neither job installs; the section below says how to run it;
4. runs each package's suite from its own directory.

The suites are `ci.yml`'s, with these real differences: one Python version (3.12; `ci.yml` runs
3.11 and 3.12), all four packages installed into one environment (`ci.yml` installs what each job
needs, so a conflict between the packages shows up only here), and none of the
`package-metadata` job (the builds, `twine check`, `tools/tests` and the non-ASCII check).

The step ends in one `result`, and only an exact `pass` leads to a pull request:

| `result` | Means | Leads to |
|---|---|---|
| `pass` | Every suite passed. | The pull request. |
| `fail` | A suite failed. | The issue "... breaks the adapter" with the failing tests. |
| `install-failed` | pip could not install the new packages. | The issue "... install failed" with the pip log. |
| `setup-failed` | The job stopped before any suite ran, for a reason that is not a failing test and not pip. | The issue "... setup failed" with the reason. |

`setup-failed` has three causes, each named in the issue and none of them a pass. A person has to
look at every one: the cause may be in this repository's pins, in the job, or in TCKDB.

- The bump was refused or failed. The usual refusal is a package ahead of the target: TCKDB's
  `main` is older than a package's pin for a bound (TCKDB went back, or the package was moved by
  hand). The issue carries the tool's error text, which names the package, its `pyproject.toml`
  and the lines (`refusing to move tckdb-schemas in <file> back from X.Y to X.Z`), and the
  versions TCKDB's `main` has; a person decides whether the package comes back or the others
  move up (`--bump --allow-downgrade` moves every package to the target). Any other failure of
  the bump is reported with the tool's error text alone.
- `--list-packages` failed or printed nothing.
- TCKDB's `backend/app/chemistry` could not be fetched at the new commit. If TCKDB moved or
  removed that directory, the adapter's comparison with TCKDB's identity rules has to follow.

Its `publish` job is the only one with write access (`tools/tckdb_drift_publish.sh`):

- **The tests pass.** It pushes the branch `chore/tckdb-drift-schemas-<X.Y>-client-<A.B>`
  and opens or updates the pull request "Track TCKDB: schemas X.Y.Z / client A.B.C (TCKDB
  <sha7>)", then starts `ci.yml` on the branch. The body carries the output of
  `python -m tckdb_schemas.contract --since <old pin>`, what `--bump` did (the floor raise
  included) and a checklist. The
  checklist asks the reviewer to complete each package's changelog entry (the two styles are
  described under "The tool").
- **The tests fail.** It opens or updates the issue "TCKDB drift: schemas X.Y / client A.B
  breaks the adapter" with the failing tests and the steps to reproduce them (the `--bump`
  command, the two TCKDB requirements from `--install-specs`, every package from
  `--list-packages` installed editable with `[test]`, the backend fetch, and each suite run
  from its own directory with `python -m pytest -p no:xdist -p no:cacheprovider`; the chemkin
  and qcschema suites are not part of a root run, see `AGENTS.md`, "Running tests"; the block has
  no comment lines, so it can be pasted into bash or an interactive zsh, fetches the backend into a fresh directory each time and runs the
  suites only if that worked). An install failure is titled "... install failed". A `setup-failed`
  issue is titled "... setup failed" and has no steps to rerun: its text names the cause.
- **The tests pass but no pull request could be made.** The issue "TCKDB drift: schemas
  X.Y / client A.B is ready to adopt" names the branch or the `--bump` command.

Every body the script sends to `gh` (issue, pull request, comment, the comment on closing an
issue) is under GitHub's limit of 65,536 characters, whatever the inputs: the failing-test
summary and the `--since` output are each cut to 20,000 bytes, and the finished body to 60,000,
with a marker that says how much was omitted (`tckdb_drift.py --cap-text`; the issue's state
marker is added after the first cut so it always survives). The summary the test job writes is
itself cut to 40,000 bytes at the end, and it lists each package's tally before its failing tests,
so the tallies survive that cut and failing-test lines are what is lost.

The dedup state below hashes the failing-test lines of that summary file as the test job wrote
it: not the displayed text, so the display cuts change nothing, but the lines that survived the
test job's own cuts (40 per package, 40,000 bytes in all). For a summary under those cuts the
hash is exactly the one the script computed before it capped any body, so no open issue gets a
spurious comment. It differs only for a summary the 40,000-byte cut shortened: before the cut
existed such a run hashed every failing-test line it listed, now it hashes the ones that remain.

The bot's pull request does not run the docs build, because the bot dispatches only
`ci.yml`; the docs build for its README changes first runs on `main` after the merge.

Pull requests and issues are keyed by the pair of release lines, so a patch release updates
the existing one. An issue gets a new comment only when the versions or the failing tests
change. A drift issue is closed when a later run finds no drift or the pull request path
succeeds. An older bot pull request is commented as superseded when a newer line appears;
it is never closed automatically.

The bot never overwrites work on its branch. If the remote branch already has the new
tree, nothing is pushed. If the branch holds a commit not authored by
`github-actions[bot]`, nothing is pushed and the pull request gets a comment with the
`--bump` command. Otherwise the branch is updated with `--force-with-lease` against the
commit the run saw.

## The tool

`tools/tckdb_drift.py` uses only the standard library.

```bash
python tools/tckdb_drift.py --check
python tools/tckdb_drift.py --check --repo /path/or/url --ref some-branch
python tools/tckdb_drift.py --bump --sha <40-hex> --schemas X.Y.Z --client A.B.C
python tools/tckdb_drift.py --since-output --from <old X.Y.0>
python tools/tckdb_drift.py --install-specs
```

`--check` prints a JSON summary on stdout and one line on stderr. It exits 0 for no drift,
10 for drift and anything else for an error. Drift means a newer minor release line; a
patch release inside the pinned line is already allowed by the bounds.

`--bump` rewrites:

- the `tckdb-schemas` and `tckdb-client` bounds in the `pyproject.toml` of every package in
  `PACKAGES` (a package that does not state one keeps not stating it, and the comments beside
  the bounds stay);
- `TARGET_SCHEMAS_LINE`;
- the `sha` in `tckdb-pin.toml`;
- the install lines and the "tested contract" sentence in `README.md`;
- the version sentence in `adapters/tckdb_arc/README.md`.

Every package moves to the target line. The bump is refused (exit 2, naming the package and its
`pyproject.toml`) when any package's line would move backwards, which includes a package that is
already ahead of the target, unless `--allow-downgrade`. It cannot leave such a package where it
is: every package states `tckdb-client` and the core states both bounds, so packages on different
lines cannot be installed into one environment, and `tools/tests/test_pin_lines.py` fails in CI
while they differ. Bringing them onto one line is a person's decision.

`--check` counts the oldest package as the pin, so it keeps reporting drift until every package
has caught up, and the bump is what catches a lagging package up (the others are already at or
level with the target, so nothing moves backwards). When the packages state different lines it
also names them in the JSON's `lines_differ` (empty when they agree) and says so on stderr; the
drift workflow does not act on it, the bump decides.

A package is released when one of its own files changed in that: its `pyproject.toml` bounds,
for `tckdb_arc` its README sentence, for the core also `TARGET_SCHEMAS_LINE`. For each package
released the tool:

- bumps the patch version in `pyproject.toml` and, where the package has one (`tckdb_core`,
  `tckdb_chemkin`, `tckdb_qcschema`), in `__version__`, so the two stay equal;
- adds a changelog entry in the package's style (`changelog_style`): `sections`, a `## X.Y.Z` section
  above the newest version heading (a heading that already starts with that version, such as
  `## X.Y.Z (unreleased)`, stops the bump instead of adding a second one); or `marker`, one line
  under the `<!-- tckdb-drift:changelog -->` marker.

When the core is released, a package that requires it (`tckdb_arc`) gets its
`tckdb-adapters-core` floor raised to the new core version, and that releases it as well: the
new core carries the new bounds, and the dependant must not be released against a core that
predates them. A package nothing changed in is not released: a bump that moves only the commit
(the same release lines) releases no package, and a bump that moves only the `tckdb-schemas`
line does not release `tckdb_chemkin`, which has no such bound.

Running it twice changes nothing.

What the generated entries say is limited to what the tool knows, and differs by style:

- A `sections` entry (`tckdb_core`, `tckdb-chemkin`, `tckdb-qcschema`) names the TCKDB commit, the
  bounds that moved (and the core floor, when it was raised) and says the package's code is
  unchanged. The `tckdb-qcschema` entry also says its request body differs from the previous
  release's, because the payload records the adapter version, and its idempotency keys do not.
  The core's says what `TARGET_SCHEMAS_LINE` is now.
- A `marker` entry (`tckdb-arc`) is one line that names the TCKDB commit and the schemas and client
  versions and, when the core was released and the tool raised the package's `tckdb-adapters-core`
  floor, that floor (`; requires tckdb-adapters-core>=X.Y.Z`). It says nothing about the code.

The tool cannot know what a new contract does to a package's behaviour or payloads (a new
refusal, a rule that now applies, a changed payload and the idempotency-key consequence). That is
something only the reviewer can write; add it to the entries it applies to in the same pull
request. The publish script's checklist says the same.

`--cap-text LIMIT` copies stdin to stdout cut to LIMIT bytes with a marker (the workflow and the
publish script use it). `--since-output` needs the new `tckdb-schemas` installed. After a bump, pass `--from` the
line the pin had before. `--install-specs` prints the two pip requirement strings built
from `tckdb-pin.toml`.

## Checking the level-of-theory hash against TCKDB

`tckdb_core.level_rules` replicates TCKDB's level-of-theory identity hash, because
`tckdb-schemas` does not ship it. Two tests in
`adapters/tckdb_arc/tests/test_energy_level_declaration.py` compare the replica with TCKDB's code,
and they are different checks:

- `test_hash_matches_the_backend_payload_rules` runs wherever `TCKDB_BACKEND_PATH` names a
  TCKDB `backend/` directory. It needs only the backend's pure `app.chemistry` modules, so
  it rebuilds the hashed payload from TCKDB's identity keys and compares the final hashing
  step replica against replica. CI runs this one, and so does the drift job (at the new commit).
- `test_hash_matches_the_real_backend_function` imports TCKDB's own
  `_level_of_theory_hash` and compares the replica with it over the test corpora and a few
  thousand combinations of method, basis, dispersion, solvent, spin and qualifiers. It
  needs the backend's web dependencies and skips, naming the reason, when that import
  fails.

Run both in an environment that has the TCKDB backend installed, with a checkout at the
pinned commit:

```bash
TCKDB=/path/to/TCKDB
cd adapters/tckdb_arc
TCKDB_BACKEND_PATH=$TCKDB/backend TCKDB_REQUIRE_BACKEND=1 \
PYTHONPATH=$TCKDB/backend:$TCKDB/schemas/python/tckdb-schemas \
  python -m pytest tests/test_energy_level_declaration.py -k hash_matches -rs
```

Both must pass, not skip, before a pin bump is trusted.

## One-time repository settings

- In Settings > Actions > General, enable "Allow GitHub Actions to create and approve pull
  requests". Optionally create a `tckdb-drift` label; the bot applies it when it exists.
- The drift workflow runs only from the default branch, and it starts `ci.yml` on its
  branch through that workflow's `workflow_dispatch` trigger.
- GitHub disables scheduled workflows after 60 days without repository activity. Re-enable
  the workflow under Actions if the daily run stops.
