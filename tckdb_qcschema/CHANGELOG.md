# Changelog: tckdb-qcschema

Releases are listed newest first. Releases up to 0.9.7 were made from the TCKDB repository, and
their entries are in [TCKDB's changelog](https://github.com/TCKDB/TCKDB/blob/main/CHANGELOG.md).

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
