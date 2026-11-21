# Changelog: tckdb-chemkin

Releases are listed newest first. Releases up to 0.7.5 were made from the TCKDB repository, and
their entries are in [TCKDB's changelog](https://github.com/TCKDB/TCKDB/blob/main/CHANGELOG.md).

## 0.7.8

Tracks TCKDB 292e647 (tckdb-schemas 0.177.1, tckdb-client 0.210.1). Requires
`tckdb-client>=0.210,<0.211` (was `>=0.209,<0.210`). Package code is unchanged.

## 0.7.7

Requires `tckdb-client>=0.209,<0.210` (was `>=0.208,<0.209`). Parsing, identity resolution,
payloads and idempotency keys are unchanged.

## 0.7.6

The package lives in this repository, at `tckdb_chemkin/`, with its history. Parsing, identity
resolution, payloads, dependencies and the `tckdb-chemkin-import` command are unchanged, so a
mechanism builds the same payloads and idempotency keys as under 0.7.5.

- The project URLs and the README's links name the tckdb-adapters repository, and the changelog
  is this file. From a checkout, the install is `pip install -e tckdb_chemkin`.
- `tests/test_package_version.py` reads the package's own `pyproject.toml`: its name is
  `tckdb-chemkin` and it lists `tckdb-client` as a dependency. It used to read the TCKDB client's
  `pyproject.toml` two directories up.
