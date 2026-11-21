# The TCKDB pin and drift tracking

This repository is tested against one TCKDB commit. This page says where that pin lives,
how the scheduled workflow moves it, and what a person must still do when it moves.

## Where the pin lives

| Place | Holds |
|---|---|
| `tckdb-pin.toml` | The TCKDB repository URL, the commit `sha`, and the subdirectories of the two packages. CI reads it at run time, so no workflow file contains a commit. |
| `tckdb_core/pyproject.toml`, `tckdb_arc/pyproject.toml` | The `tckdb-schemas` and `tckdb-client` bounds, each one minor release line (`>=X.Y,<X.(Y+1)`). |
| `tckdb_core/tckdb_core/testing/contract.py` | `TARGET_SCHEMAS_LINE`, the `tckdb-schemas` line the test suites target. It exists once. Each package's `test_contract_pin.py` fails when the installed version or a `pyproject.toml` bound disagrees with it. |
| `README.md`, `tckdb_arc/README.md` | The install lines and the sentences that name the tested versions. |

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
branch of the TCKDB repository named in `tckdb-pin.toml`. On a newer line it bumps the
pins in the runner, installs both TCKDB packages from that commit and runs the full suite.

Its `publish` job is the only one with write access (`tools/tckdb_drift_publish.sh`):

- **The tests pass.** It pushes the branch `chore/tckdb-drift-schemas-<X.Y>-client-<A.B>`
  and opens or updates the pull request "Track TCKDB: schemas X.Y.Z / client A.B.C (TCKDB
  <sha7>)", then starts `ci.yml` on the branch. The body carries the output of
  `python -m tckdb_schemas.contract --since <old pin>` and a checklist.
- **The tests fail.** It opens or updates the issue "TCKDB drift: schemas X.Y / client A.B
  breaks the adapter" with the failing tests. An install failure is titled "... install
  failed".
- **The tests pass but no pull request could be made.** The issue "TCKDB drift: schemas
  X.Y / client A.B is ready to adopt" names the branch or the `--bump` command.

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

- the dependency bounds in both `pyproject.toml` files;
- `TARGET_SCHEMAS_LINE`;
- the `sha` in `tckdb-pin.toml`;
- the install lines and the "tested contract" sentence in `README.md`;
- the version sentence in `tckdb_arc/README.md`;
- one line under the `<!-- tckdb-drift:changelog -->` marker in `tckdb_arc/CHANGELOG.md`;
- the `tckdb-arc` patch version.

Running it twice changes nothing. It refuses to move a line backwards unless given
`--allow-downgrade`. It does not change the `tckdb-adapters-core` version or its
changelog; do that by hand in the same pull request.

`--bump` does not touch `tckdb_chemkin/pyproject.toml` or `tckdb_qcschema/pyproject.toml`.
Their `tckdb-client` (and `tckdb-schemas`) windows are their own, and CI installs both
packages against the pin, so a pin moved past a window fails that package's CI job. Move its
window by hand, bump its patch version and add a changelog entry in the same pull request.

`--since-output` needs the new `tckdb-schemas` installed. After a bump, pass `--from` the
line the pin had before. `--install-specs` prints the two pip requirement strings built
from `tckdb-pin.toml`.

## Checking the level-of-theory hash against TCKDB

`tckdb_core.level_rules` replicates TCKDB's level-of-theory identity hash, because
`tckdb-schemas` does not ship it. Two tests in
`tckdb_arc/tests/test_energy_level_declaration.py` compare the replica with TCKDB's code,
and they are different checks:

- `test_hash_matches_the_backend_payload_rules` runs wherever `TCKDB_BACKEND_PATH` names a
  TCKDB `backend/` directory. It needs only the backend's pure `app.chemistry` modules, so
  it rebuilds the hashed payload from TCKDB's identity keys and compares the final hashing
  step replica against replica. CI runs this one.
- `test_hash_matches_the_real_backend_function` imports TCKDB's own
  `_level_of_theory_hash` and compares the replica with it over the test corpora and a few
  thousand combinations of method, basis, dispersion, solvent, spin and qualifiers. It
  needs the backend's web dependencies and skips, naming the reason, when that import
  fails.

Run both in an environment that has the TCKDB backend installed, with a checkout at the
pinned commit:

```bash
TCKDB=/path/to/TCKDB
cd tckdb_arc
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
