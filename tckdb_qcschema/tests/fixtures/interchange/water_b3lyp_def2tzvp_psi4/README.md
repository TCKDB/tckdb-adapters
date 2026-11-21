# Water B3LYP/def2-TZVP Hessian from Psi4

Two documents the mapping-report tests (`tests/test_report_coverage.py`) import:

| File | What it is |
| --- | --- |
| `water_b3lyp_def2tzvp_hessian_v1.json` | qcengine's `AtomicResult` (QCSchema family v1), driver `hessian`, as written |
| `water_b3lyp_def2tzvp_hessian_v2.json` | the same result converted with qcelemental's `convert_v(2)` (family v2) |

They are byte-identical copies of the inputs to TCKDB's QCSchema interchange
demonstration, `backend/tests/fixtures/qcschema/water_b3lyp_def2tzvp_psi4/` in
[TCKDB](https://github.com/TCKDB/TCKDB). That directory keeps the script that
produced them, the Gaussian reference they are compared with, and a `meta.json`
with the sha256 of each file:

- v1: `a1599b6b5d35641998fd34ffc7e8313001e5bf21a2236de74cc6a655e0a61ae2`
- v2: `3ecc23f470b86d6eec0da621f612dcd85f7f868b01688841189140a4bfdaf4a9`

Psi4 1.11 through qcengine 0.51.0, with qcelemental 0.51.2, in the environment
`../../ENVIRONMENT.lock.txt` describes.
