# Contributing

Bug reports and pull requests are welcome. This page covers what a pull request needs.
[`docs/writing_an_adapter.md`](docs/writing_an_adapter.md) covers adding a new adapter,
and [`AGENTS.md`](AGENTS.md) states the repository's rules in short form.

## Set up

```bash
python tools/tckdb_drift.py --install-specs      # prints the two pinned TCKDB requirements
pip install "<TCKDB_CLIENT_SPEC value>" "<TCKDB_SCHEMAS_SPEC value>"
pip install -e "tckdb_core[test]" -e "tckdb_arc[test]"   # and each further adapter
python -m pytest
pip install -e "tckdb_chemkin[test]" -e "tckdb_qcschema[test]"   # when changing either
(cd tckdb_chemkin && python -m pytest)
(cd tckdb_qcschema && python -m pytest)
```

Python 3.11 or newer. ARC is optional; the tests that need it skip without it.

The documentation site is built from `docs/`, `mkdocs.yml`, the READMEs, the changelogs and
this file. Install its requirements in a separate virtualenv, never in the environment you
run the tests in, then build or preview it:

```bash
pip install -r requirements-docs.txt
mkdocs serve             # preview at http://127.0.0.1:8000
mkdocs build --strict    # the check CI runs; writes site/
```

## The rules a change must follow

1. **Read the contract before changing a mapping.** Run
   `python -m tckdb_schemas.contract --print` and work from what it says.
2. **Send only what the source states.** A missing convention is omitted or the block is
   refused, with a registered warning code and a test. No default is filled in.
3. **Mark interpretations.** If a change makes the adapter state something the source
   file does not, the payload and the sidecar must say it is the adapter's interpretation.
   Open an issue first; that is a decision for the maintainers.
4. **Keep the core free of any one tool.** Nothing under `tckdb_core/` imports or names a
   producer.
5. **Import rules that `tckdb-schemas` ships.** Do not copy them.

## What a pull request contains

- Tests for the change. Every payload a test builds is validated against the shipped JSON
  Schema by the `conftest.py` hook; add a `contract_validate` call where a test checks a
  fragment.
- A patch version bump and a `CHANGELOG.md` entry for each package whose code changed.
  Say so when the release changes a payload, because that changes idempotency keys.
- The regenerated warning-code page (`docs/contract/WARNING_CODES.md` for ARC) when a warning
  code is added or changed (`python tools/gen_warning_codes.py`).
- An updated `docs/contract/ARC_MAPPING.md` when the ARC mapping changes.
- ASCII in every string the code raises, logs or records as a warning
  (`python tools/check_runtime_ascii.py`).

Run the full suite once before opening the pull request. CI runs every package's suite on Python
3.11 and 3.12 with the TCKDB packages installed from the pinned commit, and builds every
distribution with `twine check --strict`.

## TCKDB pin bumps

A scheduled workflow opens pull requests named "Track TCKDB: ...". Reviewing one means
reading the contract changelog in its body, as [`docs/tckdb_pin.md`](docs/tckdb_pin.md)
describes. The passing test run alone is not the review.

## Never in a test or a tool

No test, tool or workflow in this repository contacts a shared or production TCKDB server.
The live gate accepts only an isolated backend on the local machine.

## Changelogs

Each package keeps its own `CHANGELOG.md`, newest release first, one section per version.
`tools/tckdb_drift.py --bump` adds the entry for a pin bump to the `CHANGELOG.md` of each package it releases.
The repository has no changelog of its own; documentation and tooling changes are
described in commit messages.
