Real path-search outputs for `test_path_search_points.py`. Trimmed copies, no paths or
identifiers beyond what ORCA and xTB print.

* `orca_6_0_neb_ts/input.log`: ORCA 6.0.0 `NEB-TS` run by ARC's `orca_neb` adapter for
  i-C3H7 <=> n-C3H7 (wB97X-D3/def2-TZVP, 15 intermediate images). Kept: the header, the input
  echo, the NEB optimization through the saddle-point block, the `PATH SUMMARY FOR NEB-TS`
  block and the termination line. The end-point pre-optimizations and the TS optimization are
  cut.
* `orca_6_1_neb_ci/orca.out`: ORCA 6.1.0 `NEB-CI` run (100 intermediate images), cut the same
  way from TCKDB's `backend/tests/fixtures/orca/neb_ci_orca.out`.
* `xtb_gsm_ic3h7/`: ARC's `xtb_gsm` run for the same reaction: `stringfile.xyz0000` and the
  per-node xTB `energy`/`gradient` files ARC's `ograd` wrapper keeps in `gsm_node_outputs/`.
  `gsm_evidence.json` is the envelope ARC's `arc.parser_evidence._build_gsm` builds from them
  (ARC `e4b58e17f`); it matches each node evaluation to a stringfile frame by geometry.
