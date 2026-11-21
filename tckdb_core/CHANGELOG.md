# Changelog: tckdb-adapters-core

Releases are listed newest first. Each `tckdb-arc` release names the core version it needs in its
own changelog.

## 0.5.12

The submit sequence every route shares moves into the base class. No payload, endpoint,
idempotency key, file name or sidecar changes.

- `TCKDBUploaderBase._write_and_upload(*, payload, kind, endpoint, label, project_label,
  species_label, conformer_label, log_name="", subdir=None, warnings=None, is_partial=False,
  attach_rights=True)`: sets `config.rights` on the payload (overwriting a `rights` it has),
  builds the idempotency key from the finished payload in the producer's namespace, writes the
  payload and a `pending` sidecar, logs the write, and finishes as `skipped` when upload is off
  (always, for a partial record) or uploads through `_upload`. A producer's submit method builds
  and checks the payload, then returns this call.
- `TCKDBUploaderBase._attach_rights(payload)` and `_idempotency_key(payload, *, kind,
  project_label, species_label, conformer_label)` are now methods of the core class.
- `submit_standalone` ends in `_write_and_upload`. Its "payload written" log line has the same
  message as before, but the record's `msg` now carries the route kind (`TCKDB thermo payload
  written: %s (key=%s)`) and its `args` are `(path, key)` instead of `(kind, path, key)`.

## 0.5.11

Adds the standalone record routes. No payload an existing route sends changes, and no existing
idempotency key changes.

- `tckdb_core.standalone` and `TCKDBUploaderBase.submit_standalone(kind, payload, *, label,
  qualifier=None, project_label=None, warnings=None)`: post a request a producer already built to
  `POST /uploads/thermo`, `/uploads/kinetics`, `/uploads/statmech`, `/uploads/transport`,
  `/uploads/reactions`, `/uploads/networks` or `/uploads/networks/pdep`. It adds `config.rights`
  (a request stating different rights is refused), validates the request against the route's shipped
  JSON Schema and raises `StandalonePayloadError` before anything is written when it fails, then
  writes the payload and sidecar and uploads with the same readiness probe, idempotency key,
  sidecar bookkeeping and strict mode as the other routes. The core builds none of these requests.
  The schema checks shape only; the route handlers' rules are not run. tckdb-schemas publishes no
  pydantic model for these requests.
- New constants for each route (`THERMO_ENDPOINT`/`THERMO_KIND`, `KINETICS_*`, `STATMECH_*`,
  `TRANSPORT_*`, `REACTION_*`, `NETWORK_*`, `NETWORK_PDEP_*`), a `PayloadWriter` subdirectory for
  each, and `standalone.STANDALONE_ROUTES`. Sidecars carry the kind as `payload_kind`. As with
  every sidecar the core writes, `tckdb-client` 0.208 replay does not replay them (it accepts only an
  integer `bundle_format_version` of 1; the core writes `"0"`).
- Test kit: `ROUTE_SCHEMAS` gains the seven routes. `assert_request_is_accepted` runs the pydantic
  model only for a route that has one in `REQUEST_MODELS`, then the JSON Schema. An adapter's own
  pin test, which names its routes, is unaffected; the core's pin test requires an endpoint constant
  for each entry.
- `jsonschema>=4.18` is now a runtime dependency (it was in the `[test]` extra, which no longer lists it).
- A `qualifier` is part of the payload's file name as well as its key, so two records under one label do not overwrite each other. A finding's message is cut at 200 characters, and a label that makes the key exceed 200 characters raises `StandalonePayloadError`.
- Not added: `POST /uploads/thermoml` (a ThermoML document ingest), the contribution bundle routes, the
  submission rights-attestation and supersede routes, and `/jobs/*`.

## 0.5.10

No behaviour change for existing callers; released with adapter 0.15.10.

- `TCKDBUploaderBase` gains `submit_artifacts_for_calculation` and
  `submit_artifact_batch_for_calculation`, the standalone artifact submit path
  (`POST /calculations/{calculation_ref}/artifacts`). They take the species label, the
  calculation's `calculation_ref` (and optionally its integer id) and `(kind, file_path)` pairs;
  the project label comes from the config or from `_project_label_for(output_doc)`. The skip
  rules, sidecars, batch grouping and idempotency keys are those the pipeline already used.

## 0.5.9

No behaviour change for existing callers; released with adapter 0.15.9.

- Helpers that a producer package imports now have public names. The underscore names remain as
  aliases of the same objects: `keys.safe_key_part`, `keys.local_key_for_actor`,
  `keys.calc_prefix_for_actor`, `keys.KEY_PART_RE`, `isotopes.xyz_symbols`,
  `isotopes.SMILES_ISOTOPE_RE`, `payload_writer.utcnow_iso`, `outcomes.PreparedArtifactUpload`,
  `outcomes.ArtifactBatchResult`, `constants.OUTPUT_LOG_ALLOWED_EXTS`,
  `uploader.coerce_artifact_filename`, `uploader.artifact_batch_digest`,
  `uploader.artifact_batch_idempotency_prefix` and `uploader.preflight_sleep`. The two classes'
  `__name__` changed with them.
- `config.VALID_ARTIFACT_KINDS` is read from `tckdb_schemas.enums.ArtifactKind` and so gains
  `hessian`, which the hand-kept copy lacked. `constants.VALID_TCKDB_ORIGIN_KINDS` and
  `constants.OUTPUT_LOG_ALLOWED_EXTS` are read from tckdb-schemas too; their values are unchanged.
- Test kit: `tckdb_core.testing.stubs` (`StubClient`, `StubResponse`, `SequencedReadyzClient`,
  `upload_calls`) and `tckdb_core.testing.contract_pin` (the four assertions of a package's
  contract-pin test; the route assertion checks the routes an adapter names, which can be a
  subset of the kit's table).
- The core calls its helpers by their public names, so patching the public name in a test
  takes effect.
- The packages ship a `NOTICE` that attributes the code and data ported from ARC.

## 0.5.8

No behaviour change in this package; released with adapter 0.15.8.

## 0.5.7

No behaviour change in this package; released with adapter 0.15.7. The tckdb-schemas and client bounds and `TARGET_SCHEMAS_LINE` moved to 0.175 / 0.208.

## 0.5.6

`TCKDBConfig.rights` holds the upload's `rights` fragment (license, the depositor's attestation, optional source terms); `None` (the default) sends none.

## 0.5.5

Tracks tckdb-schemas 0.174 / client 0.206 (TCKDB c20b16a9). The method-family classifier is imported from `tckdb_schemas.method_families` (the stop-gap replica is deleted; `tckdb_core.method_families` keeps `method_family` and `ONE_ELECTRON_EXEMPT_FAMILIES` as a thin surface), and `level_rules.method_identity_key` is `tckdb_schemas.method_names.method_identity_key`. No behaviour change. `TARGET_SCHEMAS_LINE` moves to `(0, 174)`.

## 0.5.4

`rules.kinetics_energy_level_assessment` accepts a one-electron participant's Hartree-Fock energy under a correlated kinetics level (method differs, every other identity field equal; `electron_counts` and `stated_electron_counts` are new optional arguments, and `KineticsLevelAssessment.one_electron_hf_calculations` names the substituted links), and `composition.electron_count` computes a species' electrons from its symbols and charge. With no electron counts passed the behaviour is unchanged. Mirrors a TCKDB rule not in the current pin (5557ed9a); do not pin this release before a TCKDB revision that carries it.

## 0.5.3

Tracks tckdb-schemas 0.170 / client 0.201 (TCKDB 5557ed9, the mirror's head). No behaviour change: the nine releases 0.162 to 0.170 in this pin (0.171 is read ahead) add refusal and warning codes, a Hessian-method refinement and read-side fields, none of which this package builds. `TARGET_SCHEMAS_LINE` moves to `(0, 170)`.

## 0.5.2

No contract change (same pin). `route_level` documents that its parser is Gaussian-only: callers compare a route with a level only when the record states the job ran in Gaussian, because an ORCA keyword line carries auxiliary-basis tokens with a slash (`aug-cc-pvtz/c`, `def2/J`) that are not a `method/basis` pair. The functions are unchanged.

## 0.5.1

Tracks tckdb-schemas 0.161 / client 0.189 (TCKDB cd84fb6, #870). `rules.kinetics_energy_level_assessment` (and `kinetics_energy_level_declaration`, which returns its `level`) now mirrors TCKDB's per-participant spin rule instead of requiring every linked energy at one identical level: the links must agree on every identity field except `spin_treatment`; closed-shell links never constrain spin; the open-shell links name the declared `spin_treatment`, and open-shell links with different references declare nothing and are reported in `mixed_spin_calculations`. It needs two more facts per call, `multiplicities` (calculation key to the owning participant's multiplicity) and each calculation's own `spin_diagnostic.s_squared`. Breaking for direct callers: `multiplicities` is a required argument.

## 0.5.0

Tracks tckdb-schemas 0.158 / client 0.187 (TCKDB 531f823, #865 typed method qualifiers). The level-identity replica (`level_rules`) follows TCKDB's hash: the `keywords` key is gone (free-text keywords left `LevelOfTheoryRef`, so every level hashes differently from before, as on the server), and `relativistic_hamiltonian`, `pno_truncation` and `ri_approximation` join the payload only when stated, as `core_treatment` does (which now also accepts `window`); an explicit `none` is a stated value. `tckdb_core.testing.backend_import.backend_level_hash` builds the same payload. A level that states no qualifier hashes as the server does (pinned to TCKDB's own literals in `tests/test_level_rules.py`).

## 0.4.3

Tracks tckdb-schemas 0.156 / client 0.186 (TCKDB 6a91b38); no behaviour change in this package.

## 0.4.2

A `/readyz` preflight that fails without an HTTP response (connect timeout, connection refused) now says why: the exception text, its type chain, the `base_url` host and the request timeout, instead of nothing after the colon; HTTP-level messages are unchanged.

## 0.4.1

Tracks tckdb-schemas 0.154 / client 0.184 (the pin of 0.4.0); no behaviour change in this package.

## 0.4.0

New rules written against the tckdb-schemas 0.74 to 0.134 contract (the tested line is 0.154 / client 0.184). New pre-checks in `tckdb_core.rules`: `geometry_anchor_refusals` (the
0.121 key rules, run through the shipped `bundle_source_rules` helpers), `undeclarable_input_geometries` (a replica of
`calculation_input_geometry_mismatch`) and `kinetics_energy_level_declaration` (0.99); in `tckdb_core.isotopes`:
`subject_isotope_multiset` and `unlabelled_point_geometries` (0.94). The uploader no longer posts artifacts for a
calculation without a `calc_` ref (0.116), and `CoreWarning.CALCULATION_REF_NOT_RETURNED` now says so; the
`_calculation_ref_not_returned_warning` helper is gone. `TARGET_SCHEMAS_LINE` is `(0, 154)` (set by the 0.3.0 pin bump).

## 0.2.0

From the ARC adapter, with no behaviour change: the thermo numerics, the RMG unit tables, the
reaction-route result flattening, the element/formula/xyz helpers and the energy constant, the isotope
and route-line rules, the software-banner split, the TCKDB rule pre-checks (energy-level declaration,
reaction-coordinate designation, evidence shapes and their offline replica), the contract test kit and
the live-gate scaffolding, the warning-code registry type, and per-producer logging. What reads one of a
producer's own keys (ARC's `ts_checks`, `xyz_isotopes`, `thermo.*` fields, level dicts) stayed in
`tckdb_arc` and calls these with its facts as arguments.
