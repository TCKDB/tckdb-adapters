# Changelog: tckdb-qcschema

Releases are listed newest first. Releases up to 0.9.7 were made from the TCKDB repository, and
their entries are in [TCKDB's changelog](https://github.com/TCKDB/TCKDB/blob/main/CHANGELOG.md).

## 0.9.13

Tracks TCKDB edb15a2 (tckdb-schemas 0.177.5, tckdb-client 0.213.0). Requires
`tckdb-client>=0.213,<0.214` (was `>=0.211,<0.212`). Package code is unchanged. The payload records
the adapter version, so its request body differs from 0.9.12's; the idempotency keys do not.

## 0.9.12

Tracks TCKDB 67a812c (tckdb-schemas 0.177.3, tckdb-client 0.211.0). Requires
`tckdb-client>=0.211,<0.212` (was `>=0.210,<0.211`). Package code is unchanged. The payload records
the adapter version, so its request body differs from 0.9.11's; the idempotency keys do not.

## 0.9.11

The package moved from `tckdb_qcschema/` to `adapters/tckdb_qcschema/` in the repository. The
`Source` and `Changelog` project URLs and the README's source and changelog links name the new
path, and the checkout install is `pip install -e adapters/tckdb_qcschema`; those metadata edits
are the reason for the release. Reading, mapping, export logic, dependencies and the
`tckdb-qcschema` command are unchanged. The adapter version is recorded in what the package
produces, so the output differs from 0.9.10's only where it records that version: an imported
document's request body (the result origin's `producer`, the parser stamp and `adapter_version`
in `calculation.parameters_json`) and an exported document's `provenance.routine` and
`export_adapter_version`. The idempotency keys do not change.

The unshipped scripts follow the move: `scripts/emit_backend_corpus.py` records its source
fixtures as `tckdb-adapters:adapters/tckdb_qcschema/tests/fixtures/<case>`, and
`scripts/capture_read_fixtures.py` and `scripts/capture_scan_read_fixtures.py` write the captured
reads under `adapters/tckdb_qcschema/tests/fixtures/` of the checkout named by
`TCKDB_ADAPTERS_ROOT`.

## 0.9.10

Tracks TCKDB 292e647 (tckdb-schemas 0.177.1, tckdb-client 0.210.1). Requires
`tckdb-client>=0.210,<0.211` and `tckdb-schemas>=0.177,<0.178` (were `>=0.209,<0.210` and
`>=0.176,<0.177`). Package code is unchanged. The payload records the adapter version, so its
request body differs from 0.9.9's; the idempotency keys do not.

## 0.9.9

Requires `tckdb-client>=0.209,<0.210` and `tckdb-schemas>=0.176,<0.177` (were `>=0.208,<0.209`
and `>=0.175,<0.176`). Reading, mapping and export are unchanged. The payload records the adapter
version, so its request body differs from 0.9.8's; the idempotency keys do not.

## 0.9.8

The package lives in this repository, at `tckdb_qcschema/`, with its history. Reading, mapping,
export, dependencies and the `tckdb-qcschema` command are unchanged.

- The payload records the adapter version (the result origin's `producer`, the parser stamp, and
  `adapter_version` in `calculation.parameters_json`), so a document imported under 0.9.8 has a
  different request body than under 0.9.7. The idempotency keys come from the document, not the
  payload, and are unchanged.
- The project URLs and the README's links name the tckdb-adapters repository, and the changelog
  is this file. From a checkout, the install is `pip install -e tckdb_qcschema`.
- `tests/test_report_coverage.py` reads the Psi4 B3LYP/def2-TZVP water Hessian (families v1 and
  v2) from `tests/fixtures/interchange/`, byte-identical copies of the documents in TCKDB's
  `backend/tests/fixtures/qcschema/water_b3lyp_def2tzvp_psi4/`. It no longer reads TCKDB's backend
  corpus: its eight route documents are byte-identical to the adapter's own fixtures, which the test
  already covers.
- `tests/test_package_version.py` reads the package's own `pyproject.toml` instead of the TCKDB
  client's.
- `scripts/emit_backend_corpus.py` takes `--tckdb <TCKDB checkout>` for the corpus it writes, and
  records its source fixtures as `tckdb-adapters:tckdb_qcschema/tests/fixtures/<case>`.
  `scripts/capture_read_fixtures.py` and `scripts/capture_scan_read_fixtures.py`, which run from a
  TCKDB checkout, write the captured reads into the tckdb-adapters checkout named by
  `TCKDB_ADAPTERS_ROOT`.
