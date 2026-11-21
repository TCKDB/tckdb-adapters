# Changelog: tckdb-adapters-core

Releases are listed newest first. Each `tckdb-arc` release names the core version it needs in its
own changelog.

## 0.5.17

No payload, idempotency key or sidecar change. Log messages from the thermo numerics and the
constraint builder use `--` where they had an em dash, so every string the core raises or logs is
ASCII (`tools/check_runtime_ascii.py`).

## 0.5.16

Requires `tckdb-client>=0.209,<0.210` and `tckdb-schemas>=0.176,<0.177` (were `>=0.208,<0.209`
and `>=0.175,<0.176`), and the contract kit's `TARGET_SCHEMAS_LINE` is 0.176. No payload,
idempotency key or sidecar changes.

## 0.5.15

A calculation's artifacts are cut into several requests when its files total more than 64 MiB,
the way `tckdb-client` cuts them. For a calculation whose files total 64 MiB or less, and a plan
of one calculation (what every public submit method sends), the request, body, key and sidecar
are unchanged. A calculation over 64 MiB now goes out as several requests, the first under the
old idempotency key and the rest under new keys. Atomicity is per request, not per calculation.

Two consequences of how the keys are made, the same as in `TCKDBClient.upload_artifacts`. Only
the first request's key carries the digest of the whole plan
(`<prefix>:<calculation ref>-<digest>:artifact-batch`); a later request's key is
`<prefix>:<kind>-<first 16 hex of its first file's sha256>:artifact-batch-N`. So if a later run
sends a different set of files in a later request whose first file is unchanged, the key repeats
with a different body and TCKDB answers 409 `idempotency_conflict` for that request. Likewise a
calculation over 64 MiB that an earlier core version stored as one request now sends its first
request under the same key with a smaller body, and gets 409 `idempotency_conflict`.

- The request plan (`uploader._artifact_batch_requests`) groups a plan's items by calculation, in
  first-seen order, and starts a new request when adding the next file would take the running
  total of file sizes on disk past `tckdb_client.builders.artifact.INLINE_TOTAL_BUDGET_BYTES`,
  read from the client at call time (the number is not copied here). A file over the budget on its
  own is sent alone. Item order is kept. `_artifact_batch_bodies` yields one `(handle, group, body)`
  per request and builds each body only when it is posted.
- The first request of a calculation keeps its key, `<prefix>:<first calculation key>:artifact-batch`;
  later requests end in `artifact-batch-2`, `artifact-batch-3`, and so on, each through
  `validate_idempotency_key`. The key prefix and `artifact_batch_digest` are unchanged and are still
  computed over every item of the submit call.
- When request k of N fails, the items of the requests before it are recorded as `uploaded` with
  their own request's response, status, request id and warnings; the items of the failed request
  are recorded as `failed` with the error; the items of the requests never sent are recorded as
  `failed` with a `last_error` that says they were not sent because an earlier request failed.
  Strict mode raises after every sidecar is written, so a caller that counts on the exception
  treats the whole calculation as failed although the sidecars of the earlier requests say
  `uploaded`. Before, a failure marked every item failed, which was true while a calculation was
  one request.
- A plan that spans several calculations is reachable only through the private
  `_upload_artifact_batch`. For it, a sidecar's `response_body` and `public_refs` now describe its
  own request, not every request of the plan, and calculations sent before a failure stay
  `uploaded`.
- A file that has gone missing or cannot be read when its request is made fails the request it
  would have joined (the files already waiting for it included, since the request's cut is not
  known without its size); requests that closed before it stay `uploaded` and the later ones are
  recorded as not sent. File sizes are read from disk when the request is made, not taken from the
  prepared `bytes`.
- The pre-upload validation still checks every item before the first request is sent.
- `_post_artifact_batches` returns `(sent, failure)` instead of a list and no longer raises for a
  failed request; its docstring says what `TCKDBClient.upload_artifacts` returns (the body with the
  status, request id and replay flag, but not the headers) and why the core sends through
  `request_json`. `testing.contract.artifacts_request_bodies` cuts a plan the same way.
- `calculation_id` may still be `None`: a calculation is addressed by its `calc_` ref.

## 0.5.14

A failed upload is retried when the failure is transient, and records the server's error code.
No payload any route builds changes, so no idempotency key changes.

- The client `TCKDBUploaderBase._make_client` builds now carries a `tckdb_client.retry.RetryPolicy`.
  New config fields `retry_max_attempts` (default 3, the first attempt included) and
  `retry_backoff_seconds` (default 1.0, the client's `backoff_base`); the first must be an integer
  from 1 to 10 (`config.MAX_RETRY_ATTEMPTS`) and the second a finite number of at least 0, or `TCKDBConfig` raises
  `ValueError`. An upload therefore now makes up to three attempts when it meets a connection
  failure, a timeout, or a 429, 502, 503 or 504, and each replay carries the same
  `Idempotency-Key`. Which requests, statuses and error codes are replayed, the jitter, the cap and
  the `Retry-After` handling are the client's own defaults and are not set here.
  `retry_max_attempts = 1` sends no policy and restores the earlier behaviour.
- A `client_factory` still builds the whole client; the core passes it no policy.
- `timeout_seconds` applies to each phase of a request (connect, each read), not to the whole
  request. A request that makes no progress is given up after `timeout_seconds`, so with the
  defaults one upload can then take about three such timeouts plus the waits; a slow but progressing
  large upload can run longer on each attempt. Each wait is jittered separately and capped at 30 s:
  the first is between 0.5 and 1 times `retry_backoff_seconds`, and the un-jittered wait doubles
  each time. When the server sends `Retry-After` the client waits at least that long, up to 120 s
  per wait, and does not replay at all above 120 s. The readiness probe runs once per uploader, so a
  server that degrades mid-run costs this on every remaining upload.
- TCKDB does not lock an idempotency key while a request is in progress. After a client-side timeout
  both the original request and the replay can run, and whichever commits second is refused with
  `409 idempotency_conflict` and rolled back, so no duplicate record is created. Either the original
  or the replay can be the one refused, so the sidecar can be `failed` with `server_error_code:
  idempotency_conflict` although the data was stored. Running the upload again later returns the
  stored response when the server stored a receipt for the write; a response that carries
  `Idempotency-Receipt: failed` means it did not, and a later run executes the write again.
- The readiness probe is not retried by the client: it keeps its own five attempts and its own
  waits, so its worst case is what it was. When the core builds the client and a policy is set,
  the probe goes through a second client without one (same URL, key and timeout), closed when the
  probe ends; with a `client_factory`, a subclass that overrides `_make_client`, or `retry_max_attempts = 1`,
  the probe uses the one client. The API key is resolved once and handed to the probe client.
- `TCKDBUploaderBase._sleep_between_retries(seconds)` (backed by `uploader.retry_sleep`) is the
  wait between the client's replays; a test rebinds it, as it does `_sleep_between_probes`.
- When a failed upload's exception carries a `code` string (`TCKDBHTTPError.code`), the sidecar
  records it as `server_error_code` and `UploadOutcome` and `ArtifactUploadOutcome` carry the same
  field, for every route and for artifact batches. It is `null` for a connection failure, a
  readiness failure, a locally refused request and a success. Every sidecar now has this key (a
  `null` when unset); other keys are unchanged. `last_error` is unchanged. The number of attempts
  is not recorded: the client does not report it.

## 0.5.13

Checks every request against the shipped contract before it is posted. No payload any route builds
changes, and no idempotency key changes; a request the contract refuses is no longer posted.

- New module `tckdb_core.validation`: `ROUTE_SCHEMAS` (endpoint to contract surface, now the one
  table for all twelve routes; `tckdb_core.testing.contract` and `tckdb_core.standalone` read it
  instead of keeping their own), the cached `Draft202012Validator` per schema, the published
  pydantic models (`request_models()`, `REQUEST_MODELS`, `ARTIFACT_ITEM_MODEL`),
  `request_findings` / `schema_findings` and `PayloadContractError`. Each finding is one line,
  `"<path>: <message>"`: JSON Schema messages are cut at 200 characters (they echo the value),
  a model's prose at 2000, at most 200 findings. jsonschema, the shipped schemas and the models are
  imported on the first check, not by `import tckdb_core.uploader`.
- Public names. Every name `testing.contract` and `standalone` exposed before still resolves
  (`ROUTE_SCHEMAS`, `REQUEST_MODELS`, the four request models and `ArtifactIn` in the kit,
  `MAX_FINDING_CHARS`, `_clip`, `_validator`, `StandalonePayloadError`, ...). Only incidental imports
  are gone: `json` from the kit, and `json`, `lru_cache`, `Draft202012Validator` and `contract`
  from `standalone`. `StandalonePayloadError` is now a subclass of `PayloadContractError`.
- Which check runs. At run time the published model alone decides for a route that has one (the
  conformer, computed-species, computed-reaction and transition-state routes, and each item of an
  artifact batch), because the server runs that model in pydantic's lax mode: `species_entry.charge:
  "0"` is accepted by the server and by the model, and refused by the JSON Schema ("not of type
  integer"). A route with no published model (the seven standalone record routes) is checked
  against its shipped JSON Schema. The test kit's `assert_request_is_accepted` is unchanged and
  stricter (model, then schema), so a builder stays held to the schema in tests.
- `TCKDBUploaderBase._upload` and the artifact batch upload check the request first, before the
  API-key lookup, the readiness probe and the POST. A request that fails is not posted. The payload
  file stays on disk; the sidecar is `failed` with `last_error` starting `payload_failed_local_validation:`
  and the first finding (so the one line a sweep prints says what disagreed, the model's own wording
  and remedy included, without pydantic's `Value error, ` prefix), and a warning with that code
  (new `CoreWarning`; `source` is `tckdb_core_self_check`) carrying the endpoint, the contract, the
  stable refusal codes found in the findings (`refusal_codes`) and the first 20 findings in full.
  `response_status_code` stays empty, so it cannot be read as a server refusal. The outcome is
  `failed`; strict mode raises `PayloadContractError` after the sidecar is written.
- An artifact plan is checked whole before any of its batches is posted, from the prepared metadata
  (kind, filename, sha256, byte count, non-empty content) with `ArtifactIn`: no file is read or
  base64-encoded a second time.
- A bug in the checker never blocks an upload: if it raises, the error is logged, the sidecar gains
  a `payload_local_validation_unavailable` warning (new `CoreWarning`), and the request is posted
  unchecked, strict mode included.
- New config field `TCKDBConfig.validate_before_upload` (default `True`); `False` posts whatever was
  built, as before. Turn it off when the installed tckdb-schemas is not the one the server runs.
- `pydantic>=2.6` is now declared (tckdb-schemas already requires it).
- `submit_standalone` still validates before it writes anything and raises regardless of the switch.
- The check shares the limits of the test kit: no route-handler rules, no `format` keywords.
  Cost: building a validator takes 2 to 300 ms per schema, once per process; a check of the
  largest golden payload (a 78 KB computed-reaction request) takes about 11 ms.

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
