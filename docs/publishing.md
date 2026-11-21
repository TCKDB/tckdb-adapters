# Publishing the packages to PyPI

Audience: a maintainer releasing one of this repository's distributions. The
workflow is `.github/workflows/publish-python.yml`.

| Distribution | Directory | Import name | Depends on |
|---|---|---|---|
| `tckdb-adapters-core` | `tckdb_core/` | `tckdb_core` | `tckdb-client`, `tckdb-schemas` |
| `tckdb-arc` | `tckdb_arc/` | `tckdb_arc` | `tckdb-adapters-core`, `tckdb-client`, `tckdb-schemas` |
| `tckdb-chemkin` | `tckdb_chemkin/` | `tckdb_chemkin` | `tckdb-client`, RDKit |
| `tckdb-qcschema` | `tckdb_qcschema/` | `tckdb_qcschema` | `tckdb-client`, `tckdb-schemas`, `qcelemental` |

The list is `PACKAGES` in `tools/python_release.py`.
`tools/tests/test_publish_python_workflow.py` fails when the workflow's choices,
the table and the `tckdb_*/pyproject.toml` files on disk disagree.

`tckdb-client` and `tckdb-schemas` are published from the
[TCKDB repository](https://github.com/TCKDB/TCKDB), by the same kind of
workflow. Its runbook is `backend/docs/deployment/publishing_python_packages.md`
there.

## One-time setup

Uploads use PyPI trusted publishing: GitHub Actions proves its identity with an
OpenID Connect token, and no API token or secret is stored anywhere.

**Accounts.** One account on https://pypi.org and one on https://test.pypi.org,
both with two-factor authentication.

**Pending trusted publishers.** On each index: Account settings, Publishing,
"Add a new pending publisher", GitHub. One entry per distribution:

| Field | Value |
|---|---|
| PyPI Project Name | `tckdb-adapters-core`, `tckdb-arc`, `tckdb-chemkin`, `tckdb-qcschema` (one entry each) |
| Owner | the GitHub owner of this repository when the first upload runs (`TCKDB` once the repository is in the TCKDB organisation) |
| Repository name | `tckdb-adapters` |
| Workflow name | `publish-python.yml` |
| Environment name | `pypi` on pypi.org; `testpypi` on test.pypi.org |

PyPI matches the owner and repository of the run, so register the publishers
under the owner the repository has at the time of the first upload. A pending
publisher creates the project on that upload; nothing is uploaded by hand
first.

**GitHub environments.** In this repository's Settings, Environments, create
`testpypi` and `pypi`. On `pypi`, add a required reviewer and limit deployment
to tags matching `tckdb-*-v*`. Neither environment needs a secret.

## Tags

A release tag is `<dist-name>-v<version>`, for example `tckdb-chemkin-v0.7.6`
or `tckdb-adapters-core-v0.5.15`. The workflow refuses a tag when:

- its name is not one of the four distributions;
- its version differs from `[project].version` in that package's
  `pyproject.toml` at the tagged commit;
- the tagged commit is not on `main`.

Not every version bump is published: tag only the versions you mean to release.

## What a run does

1. **Build, check and smoke-test.** Resolve the package; build the sdist and
   wheel (`python -m build`); run `twine check --strict`; build the packages of
   this repository it requires (`tckdb-adapters-core` for `tckdb-arc`) from the
   same commit; install everything into a fresh virtual environment, with
   `tckdb-client`, `tckdb-schemas` and the third-party requirements from PyPI;
   import the package from site-packages, compare the installed version and
   `__version__` (where the module has one) with the release, and load each
   console script.
2. **Publish to TestPyPI** (environment `testpypi`). A version already there is
   skipped, so a dry run followed by the tag does not fail.
3. **Publish to PyPI** (environment `pypi`), only for a pushed tag and only
   after step 2 succeeded. It waits for the environment's reviewer.

Step 1 resolves `tckdb-client` and `tckdb-schemas` from PyPI, so a package
whose window names no version published there fails before anything is
uploaded. The windows follow the TCKDB pin (`docs/tckdb_pin.md`); move them to
published versions, bump the package and merge that first.

## Order

Each package must be on PyPI before anything that depends on it is uploaded:

1. `tckdb-schemas`, then `tckdb-client` (from TCKDB).
2. `tckdb-adapters-core`, `tckdb-chemkin` and `tckdb-qcschema`, in any order.
3. `tckdb-arc`.

Wait until a release is visible on `https://pypi.org/project/<name>/` before
pushing the next tag.

## Dry run on TestPyPI

Actions, "Publish Python package", "Run workflow": choose `main` and the
package. The run builds, checks and smoke-tests the package at that ref and
uploads it to TestPyPI only. Dry-run from `main`: TestPyPI keeps the first
upload of a version, so a dry run from another ref turns the later tag's
TestPyPI upload into a no-op and leaves the other ref's build there.

Then install it in a clean environment. TestPyPI does not hold the
dependencies, so they come from PyPI:

```bash
python -m venv /tmp/adapters-dry && . /tmp/adapters-dry/bin/activate
python -m pip install \
  --index-url https://test.pypi.org/simple/ \
  --extra-index-url https://pypi.org/simple/ \
  tckdb-chemkin==<version>
python -c "import tckdb_chemkin; print(tckdb_chemkin.__version__)"
```

## Releasing

```bash
git fetch origin
git tag tckdb-chemkin-v<version> origin/main
git push origin tckdb-chemkin-v<version>
```

Approve the `pypi` deployment when GitHub asks, and wait for the release to
appear on PyPI before tagging a package that depends on it.

## Mistakes

- **A bad release.** A version on PyPI can never be uploaded again, even after
  deleting it. Yank it (project, Manage, the release, Options, Yank), fix the
  problem, bump the version and release the new number. A yanked release stays
  installable by an exact pin and is skipped by everything else.
- **A tag on the wrong commit or with the wrong version.** The run fails before
  anything is uploaded. Delete the tag (`git push origin :refs/tags/<tag>`) and
  tag the right commit.
