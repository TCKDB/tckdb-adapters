# Changelog: tckdb-arc

Releases are listed newest first. A release that changes what a payload contains says so, because
a changed payload has a new hash and therefore a new idempotency key: TCKDB records a re-upload of
the same ARC run as a new deposit instead of replaying the old one.

Versions that are missing from the detailed list only moved the TCKDB pin; they are in the table
of tracked releases.

## Tracked TCKDB releases

`tools/tckdb_drift.py --bump` adds a line below the marker each time it moves the pin.

<!-- tckdb-drift:changelog -->
- Adapter 0.15.10: artifact submit methods moved to `tckdb_core`; same pin as 0.15.7 (TCKDB d4d5346).
- Adapter 0.15.9: public names for core helpers and artifact kinds read from tckdb-schemas; same pin as 0.15.7 (TCKDB d4d5346).
- Adapter 0.15.8: whole path-search paths (NEB images, GSM node energies); same pin as 0.15.7 (TCKDB d4d5346).
- Adapter 0.15.7: tracked TCKDB d4d5346 (schemas 0.175.2, client 0.208.2).
- Adapter 0.15.5: tracked TCKDB c20b16a (schemas 0.174.0, client 0.206.0).
- Adapter 0.15.4: tracked TCKDB f79e7f9 (schemas 0.173.0, client 0.205.0).
- Adapter 0.15.3: tracked TCKDB 5557ed9 (schemas 0.170.0, client 0.201.0).
- Adapter 0.15.2: ORCA routes are not compared with the level; same pin as 0.15.1 (TCKDB cd84fb6).
- Adapter 0.15.1: tracked TCKDB cd84fb6 (schemas 0.161.0, client 0.189.0).
- Adapter 0.15.0: tracked TCKDB 531f823 (schemas 0.158.0, client 0.187.0).
- Adapter 0.14.3: tracked TCKDB 6a91b38 (schemas 0.156.0, client 0.186.0).
- Adapter 0.14.1: Hessian method on the same pin (TCKDB c7c8df8, schemas 0.154.0, client 0.184.0); no new pin.
- Adapter 0.14.0: rules for tckdb-schemas 0.74 to 0.134 on the same pin (TCKDB c7c8df8, schemas 0.154.0, client 0.184.0); no new pin.
- Adapter 0.13.0: tracked TCKDB c7c8df8 (schemas 0.154.0, client 0.184.0). A Hessian now carries `parser: {name, version}` (schemas 0.130) instead of `parser_version`.
- Adapter 0.10.0: tracked TCKDB 96b71b0 (schemas 0.73.0, client 0.111.0).
- Adapter 0.6.9: tracked TCKDB f22d3a8 (schemas 0.64.0, client 0.102.0).
- Adapter 0.6.8: tracked TCKDB a515fb9 (schemas 0.58.0, client 0.98.0).

## 0.15.10

Needs `tckdb-adapters-core` `>=0.5.10,<0.6`. No payload change.

- `submit_artifacts_for_calculation` and `submit_artifact_batch_for_calculation` keep their names
  and signatures and now delegate to `TCKDBUploaderBase`, which holds the upload logic. The ARC
  adapter supplies the species label from the record and the project label from the output
  document. Request bodies, idempotency keys, sidecars, warnings and skip rules are unchanged.

## 0.15.9

Needs `tckdb-adapters-core` `>=0.5.9,<0.6`. No payload change.

- The `tckdb` block's `artifacts.kinds` accepts `hessian`. The list of valid kinds is now
  tckdb-schemas' own `ArtifactKind` enum; the hand-kept copy had fallen behind it. `hessian` is
  valid but not implemented, so it is reported at parse time like `checkpoint` and nothing is
  uploaded for it.
- Three warning messages (`conformer_level_not_stated`, `ts_guess_level_not_stated` and the
  adaptive-level omission) no longer end with a reference to an internal planning document. The
  codes and every other field are unchanged.

## 0.15.8

Ships with core 0.5.8; needs `tckdb-adapters-core` `>=0.5.8,<0.6`.

**A path-search calculation carries its whole path.** An ORCA NEB guess sends every image of the log's band `PATH SUMMARY` (`tckdb_arc.orca_neb`): `path_coordinate` from `Dist.(Ang.)`, the energy, `max_force`/`rms_force` from `max(|Fp|)`/`RMS(Fp)` in Eh/Bohr (perpendicular to the path, total at the two end points), the climbing image as `is_climbing_image` and `climbing_image_index` with its geometry and full-precision energy from the log's saddle-point block, and `converged: true` when ORCA printed `THE NEB OPTIMIZATION HAS CONVERGED`. A summary that does not end the way ORCA ends it, or whose image count differs from the run's stated count (or `NImages` + 2), is not read. No image is marked as the TS guess: ARC takes the guess from the log's last coordinate block, which for its NEB-TS runs is the saddle point ORCA optimized from the climbing image, not an image. That geometry (`opt_input_xyz`) is sent as the path search's `final` output geometry, and `note` says so; an unparseable log keeps the single guess point. An xtb GSM guess sends the per-node xTB energies and gradient norms from ARC's `parser_evidence.json`, which attaches a node evaluation to a stringfile frame only when their geometries match; a frame with no match has no energy, and the stringfile's all-zero energy column is still not read as energies. GSM `converged` stays unsent: ARC keeps no GSM output that states it. Relative energies are referenced to the lowest energy on the path, as before. Evidence: `tests/test_path_search_points.py` and its fixtures in `tests/fixtures/path_search`.

## 0.15.7

Ships with core 0.5.7; needs `tckdb-adapters-core` `>=0.5.7,<0.6`.

**A kinetics fit that ARC's Arkane run produced (output.yml states a `tunneling` for it) now sends `direction: forward`, a `determination` (whole reaction, complete, keyed within the bundle's workflow tool release and scoped to the computation) and an `applicability` (rate coefficient, elementary coefficient, high-pressure limit; claim origin `depositor_interpretation`)**, so a selection no longer reports `determination_not_declared`, `direction_not_recorded`, `applicability_not_declared` or `pressure_dependence_not_declared` for it. The phase is sent as `gas` only when no level states solvation (built calculations, header levels and each participant's own output.yml `levels` entry are all read); the determination key digests output.yml values (the fit, the links, the levels and energies), so two different fits of the same energies get different keys; direction, determination, pressure dependence and phase are the adapter's interpretation of ARC's documented Arkane provenance, not output.yml fields, so the kinetics `note` ends with one sentence saying so and the sidecar carries `kinetics_declarations_interpreted` (the schema gives `direction` and `determination` no origin field; only the applicability's `claim_origin` marks the rest). The reaction order and the whole `protocol` are not sent because ARC states none of them. **A one-electron species computed at Hartree-Fock keeps its enthalpy** when the atom-corrections level is a wavefunction or multireference level that is otherwise the same (the free H atom under DLPNO-CCSD(T)-F12 corrections), reported as `one_electron_hf_substitution`; every other level mismatch is still refused. What is and is not sent is listed in `docs/contract/ARC_MAPPING.md`.

## 0.15.6

Ships with core 0.5.6; needs `tckdb-adapters-core` `>=0.5.6,<0.6`.

**A `rights` block in the `tckdb` block is sent as the upload's `rights` fragment** on every upload kind (conformer, computed species, computed reaction, transition state), validated against `tckdb_schemas.rights.DepositRights` when the config is read (a block it refuses is a config error). Without the block no `rights` fragment is sent, as before.

## 0.15.5

Tracks tckdb-schemas 0.174 / client 0.206, TCKDB c20b16a; ships with core 0.5.5.

The adapter imports TCKDB's method-family classifier and method identity key from `tckdb_schemas` instead of replicating them; no behaviour change.

## 0.15.4

Tracks tckdb-schemas 0.173 / client 0.205, TCKDB f79e7f9; ships with core 0.5.4.

**A one-electron participant's HF energy no longer blocks the kinetics energy level.** ORCA cannot run a correlated method on one electron, so ARC runs a free H atom at HF in the same basis; the block now declares the correlated level the other participants share and reports `one_electron_hf_substitution` (with `cabs_singles_correction: absent` for an F12 level). The adapter mirrors the server rule of TCKDB #915 (tckdb-schemas 0.173), which the pin now carries; against a TCKDB without it (including the pinned mirror until it carries f79e7f9) a live server refuses such a payload (`kinetics_energy_level_contradiction`).

## 0.15.3

Tracks tckdb-schemas 0.170 / client 0.201, TCKDB 5557ed9, the mirror's head; ships with core 0.5.3; needs `tckdb-adapters-core` `>=0.5,<0.6`.

**No mapping change.** Ten releases (0.162 to 0.171) are read (0.171 is read ahead, not in this pin). Two matter to an ARC upload: 0.165 now accepts a recorded `finite_difference_gradient` against a `NumFreq` deck of a method with no analytic gradient (stored as `finite_difference_energy`, with the server warning `hessian_method_refined`; `analytic` against `NumFreq` is still refused), and 0.162 refuses a re-deposit of a reaction under a different `reaction_family` (from ARC's reaction `family`) with the code `reaction_family_conflict`.

## 0.15.2

Same pin; ships with core 0.5.2.

**An ORCA route is never compared with the level.** `level_contradicted_by_route` read the auxiliary basis of an ORCA DLPNO-CCSD(T)-F12 single point (`aug-cc-pvtz/c`) as a Gaussian `method/basis` pair, so the sp calculation was dropped and, when it was an atom's primary sp, the whole computed-reaction upload failed (`rotor_scan_rmg_rxn_1142`). The check now runs only for jobs the record states ran in Gaussian (`ess_software`). A participant's primary calculation that cannot be built still refuses the reaction, and the error now names the participant; its freq, sp and scan calculations are skipped with a warning.

## 0.15.1

Tracks tckdb-schemas 0.161 / client 0.189, TCKDB cd84fb6; needs `tckdb-adapters-core` `>=0.5,<0.6`, core 0.5.1.

**A kinetics block declares its energy level when the participants' spin treatments differ.** TCKDB 0.161 (#870) judges spin per participant: closed-shell links (multiplicity 1, restricted or restricted-open, or unrestricted with a stored `<S**2>` of at most 0.1) never constrain it, and the open-shell links must share one reference, which a stated spin treatment names. The adapter mirrors the rule (`tckdb_core.rules.kinetics_energy_level_assessment`): a reaction that mixes radicals computed unrestricted with restricted singlets (the `r3_04_h_abstraction` shape) now declares `{method, basis, spin_treatment: "unrestricted"}` and no longer draws `missing_level_of_theory_provenance`. When the open-shell links disagree (unrestricted against restricted-open) nothing is declared and the new `kinetics_energy_level_spin_mixed` warning says so. The adapter never invents a spin treatment or an `<S**2>`: an unrestricted singlet is exempt only if ARC states `sp_spin_diagnostic` for it. Releases 0.159 and 0.160 add server refusal codes only (`anonymous_body_too_large` 413, `server_response_invalid` 500). Literature on correction schemes: TCKDB kept `missing_literature_provenance`; the adapter keeps sending none.

## 0.15.0

Tracks tckdb-schemas 0.158 / client 0.187, TCKDB 531f823; needs `tckdb-adapters-core` `>=0.5,<0.6`.

**No level of theory carries `keywords` any more.** TCKDB 0.157/0.158 (#865, ADR 0024) removed `LevelOfTheoryRef.keywords`; its upload schemas forbid unknown fields, so a level with `keywords`, even `null`, is refused 422. The adapter used to flatten ARC's `args` into that field (`_arc_args_to_keywords`, deleted); it now sends nothing for them, on every projection (per-job, scheme, correction-scheme, kinetics/thermo energy and header levels). The ESS options live in the input deck and log, which TCKDB reads as calculation parameters; the route line stays a parameter (`_route_parameters`). The hash replica in `tckdb_core` follows (core 0.5.0). The typed qualifiers TCKDB added (`relativistic_hamiltonian`, `pno_truncation`, `ri_approximation`, `core_treatment: window`) are **not sent yet**: ARC's `args` do not reliably describe the deck a job ran with (`docs/contract/ARC_MAPPING.md`, "Levels of theory").

## 0.14.3

Tracks tckdb-schemas 0.156 / client 0.186, TCKDB 6a91b38; needs `tckdb-adapters-core` 0.4.3.

No payload or behaviour change; 0.155 is packaging only and 0.156 changes the whitespace set trimmed from electronic-state labels, which the adapter never sends.

## 0.14.2

Readiness failure reason; needs `tckdb-adapters-core` 0.4.2 behaviour.

A `/readyz` failure with no HTTP response (connect timeout, refused) now names the exception type chain, host and timeout in the sidecar `last_error`, the `strict=False` warning and the sweep summary; no payload change.

## 0.14.1

Hessian method; tckdb-schemas 0.154 / client 0.184, the pin of 0.14.0; needs `tckdb-adapters-core` `>=0.4,<0.5`.

The freq `hessian_method` ARC states is still
passed through unchanged (only `analytic`, `finite_difference_gradient`, `finite_difference_energy`, else omitted; never derived from the
deck). Tests pin that it agrees with the shipped input deck (TCKDB refuses a contradiction, 422
`hessian_method_contradicts_input_deck`), and that the refusal reaches the sidecar, the log and the sweep output by its code. In strict mode the
sweep's `failed:` line now carries the server's refusal code.

## 0.14.0

Needs `tckdb-adapters-core` `>=0.4,<0.5`.

The tested contract line is 0.154 / client 0.184
(the pin from 0.13.0); the yes/no table covers 0.74 to 0.134 (0.135 to 0.154 follow in adapter 0.14.1):

- **Hessian parser stamp (0.130).** A Hessian carries `parser: {name, version}` instead of `parser_version`. ARC's token
  `arc-hessian-1` is split at its own trailing version (`arc-hessian`, `1`); a stamp without that shape sends no `parser`.
  Payload hashes of documents with a Hessian change; nothing else in them does.
- **Geometry and conformer agree (0.121).** The built computed-reaction payload is checked with the shipped
  `bundle_source_rules` helpers before it is written (a finding refuses the build), and a freq/sp whose declared
  `input_geometries` are not its conformer's own is sent without them, with `calculation_input_geometry_not_declared`.
- **Isotopes on Hessian and path points (0.94).** A substituted species' Hessian is labelled with ARC's `xyz_isotopes` (omitted,
  with `geometry_isotopes_not_stated` in the sidecar, if its frame is not the record's atoms in order). A scan or IRC point under a
  substituted subject is labelled the same way when its frame has the record's element sequence in order and the calculation's
  program is Gaussian or ORCA (they keep the input atom order); any other point (path search, another element order, another
  program) has its geometry left out with `geometry_isotopes_not_stated`. Matching the element sequence does not prove which of
  several identical atoms is the substituted one.
- **Artifacts need a `calc_` ref (0.116).** A response without `calculation_ref` leaves its artifacts unposted (`skipped`,
  `calculation_ref_not_returned` in the reason); the integer-id fallback is gone, since the server refuses it.
- **Kinetics energy level (0.99).** `energy_level_of_theory` is declared on a kinetics block when every linked energy shares one
  level and ARC states it; the server warning `missing_level_of_theory_provenance` no longer fires on these corpora.
- Not changed: thermo is mapped by the adapter, never through `tckdb_client.builders` (pinned by a test, so a client default for
  `enthalpy_reference_kind` cannot change what is declared).

## 0.12.0

Core extraction; no behaviour change; needs `tckdb-adapters-core` `>=0.2,<0.3`.

- Moved to `tckdb_core` 0.2.0, each still resolvable from `tckdb_arc.adapter` / `.arc13` / `._vendor`: the NASA and
  thermo-point builders, the RMG unit tables (`arc_to_tckdb_a_units` / `arc_to_tckdb_ea_units` are thin wrappers over
  `tckdb_core.rmg_units`), the reaction-route result flattening, the element, formula and xyz helpers with `E_h_kJmol`,
  the isotope and route-line rules, the software-banner split, and the TCKDB rule pre-checks (energy-level
  declaration, reaction-coordinate window and tau rules, evidence record shapes, the offline evidence-rule replica).
  Anything that reads an ARC key (`ts_checks`, `xyz_isotopes`, `thermo.*`, level dicts) stays here and passes its
  facts to them.
- The `_W_*` constants and the inline `irc_software_not_stated` code are a documented registry,
  `tckdb_arc/warning_codes.py` (`ArcWarning`, one line each); `docs/contract/WARNING_CODES.md` is generated from it and
  the core's registry (`python tools/gen_warning_codes.py`; a test fails when it is stale). Every warning dict is built
  by `AdapterWarning`, so the sidecar shape (`code`, `message`, `field`, `context.source == "tckdb_arc_self_check"`)
  is unchanged.
- Logging: the shared pipeline logs through the adapter instance and the moved functions take the adapter module's
  `logger`, so `mock.patch("tckdb_arc.adapter.logger")` intercepts them; the core's process-global `set_logger_name`
  is gone.
- The contract test kit moved to `tckdb_core.testing` (`tests/_contract.py` is a re-export;
  `tests/conftest.py` registers the contract hook against this adapter's builders; the live gate's guards and fixture
  come from `tckdb_core.testing.live`). `TARGET_SCHEMAS_LINE` is the single line in
  `tckdb_core/tckdb_core/testing/contract.py`; `tools/tckdb_drift.py` rewrites it there.
- Deprecated: `tckdb_arc.level_rules` and `tckdb_arc.payload_writer` now forward their old public names to the
  `tckdb_core` modules of the same name with a `DeprecationWarning` (`level_rules`: `method_identity_key`, `level_hash`,
  `level_payload`, `level_identity_keys`, `basis_identity_key`, `component_identity_key`, `dispersion_identity_key`,
  `same_level`, `NAME_ALIASES`, `SUFFIX_ALIASES`, `DISPERSION_ALIASES`, `FOLDED_SUFFIXES`, `FOLDED_STEMS`;
  `payload_writer`: `PayloadWriter`, `SidecarMetadata`, `ArtifactSidecarMetadata`, `WrittenPayload`, `WrittenArtifact`,
  `BUNDLE_FORMAT_VERSION`, `should_replay_sidecar`). `tckdb_arc.constraints` keeps its whole public API
  (`TCKDBCalculationConstraint`, `serialize_constraints`) without a warning. Their private names (`_SAFE_LABEL`,
  `_utcnow_iso`, `_validate`, `_coerce`, `_HYPHEN_RULES`, `_SUFFIX_COMPILED`, ...) are gone: import them from
  `tckdb_core`. `tckdb_arc.config` and `tckdb_arc.idempotency` (and `TCKDBAdapter`, `TCKDBConfig`, `run_upload_sweep`,
  which ARC's own repository imports) are unchanged.

## 0.11.0

Core extraction; no behaviour change.

- The producer-agnostic half moved to the new `tckdb-adapters-core` package (`tckdb_core/`, import name
  `tckdb_core`; `tckdb-arc` depended on it, `>=0.1,<0.2`): the payload writer and sidecar types, idempotency-key
  composition (the key namespace is a parameter; ARC binds `"arc"`), constraints, the TCKDB level-identity replica, the
  config fields and API-key resolution, and the upload / sidecar / readiness / artifact-batch pipeline
  (`TCKDBUploaderBase`, which `TCKDBAdapter` inherits), with its outcome types and endpoint constants.
- Every payload, sidecar file, warning, idempotency key and log line is byte-identical to 0.10.0 (checked by building
  every route over eight fixtures, in all three upload modes, against both packages and diffing the JSON). Each name
  that moved is still importable from where it was (`tckdb_arc.adapter`, `.config`, `.payload_writer`, `.idempotency`,
  `.constraints`, `.level_rules`), so callers and tests are unchanged.
- ARC-specific code stays here: reading `output.yml`, the payload builders, `TCKDBConfig.from_dict` (a thin subclass of
  the core config) and the CLI/sweep. Install `tckdb_core` before `tckdb_arc` when working from the checkout
  (`pip install -e tckdb_core -e tckdb_arc`).

## 0.10.0

Tracks tckdb-schemas 0.73 / client 0.111.

- **Public refs.** Artifacts are posted to `/calculations/{calc_ref}/artifacts`, and the sweep names a
  calculation by the response's `primary_calculation.calculation_ref`. (Since 0.14.0 there is no integer-id fallback.) `UploadOutcome.submission_ref` and
  `.calculation_key_refs` carry the response's `sub_` ref and (computed-reaction) key-to-`calc_` map. The adapter makes no
  rights-attestation call.
- **Artifact sidecars and idempotency.** The artifact idempotency key is built from
  `(calculation_ref, artifact_kind, artifact_sha256)` (it was the integer id before), and the sidecar records
  `calculation_ref` beside `calculation_id` and is named after the ref. **A project uploaded before 0.10 holds integer-id
  keys, so a re-run would re-post each artifact under a new key, and TCKDB keeps one `calculation_artifact` row per upload
  even for identical bytes (it would duplicate the row).** The adapter therefore looks for the pre-0.10 sidecar
  (`<species>.calc<int>.<kind>.artifact.meta.json`, the response still carries the integer id) before building a
  ref-keyed upload; if its status is `uploaded`, its sha256 is the artifact's and it was posted to the same server, the
  artifact is skipped as already uploaded. Otherwise (no sidecar, pending or failed, changed bytes) it uploads under the
  ref key. Nothing needs deleting.
- **BAC frequency level.** Only a `bac_petersson` / `bac_melius` scheme whose record's `matched_arkane_key` is a
  `CompositeLevelOfTheory(freq=..., energy=...)` carries `frequency_level_of_theory`, taken from the key's freq half
  (tckdb-schemas 0.66: a scheme keyed on one level sends none; in RMG-database only 4 of 47 Petersson keys are composite).
  A single-level key, no key or an unparseable one sends none. If ARC's stated freq-job level disagrees with the key's
  freq half the field is omitted with a `bac_frequency_level_conflict` warning. It joins scheme identity, so a composite-keyed
  BAC is one new scheme row. Atom-energy schemes send none, and the adapter never sends `application_role: composite_delta`.
- **Method guards.** No `//` ever reaches a `level_of_theory.method` (a compound level is refused with a log line
  `level_method_is_compound`). An Arkane level that is a correction-table name (`cbs-qb3-paraskevas`, `cbsqb32023`) is sent
  as its method stem with the table name as the scheme `name` (warning `correction_table_method_split`). A calculation whose
  level method is a table name (ARC runs `cbs-qb3-paraskevas` as CBS-QB3) is sent with the stem too (logged with the original
  string). A calculation level with `//` is not built; its skip reason starts with `level_method_is_compound`.
- **G4 / G4MP2 on Gaussian 16 Revision A.03.** ARC's energy for such a composite run is the shifted 298 K value, so the
  record's sp energy, the E0-derived thermo content (H298, NASA, point H and G), statmech energy links and the kinetics
  built on it are withheld, with `g4_energy_loader_shifted_label_gaussian16_a03`, until ARC exports a correct E0. S298 and
  Cp (from statmech) are kept. Only output 1.3 records carry `ess_versions.composite`, so the guard cannot fire on an
  earlier document.
- **Identity replica.** `tckdb_arc.level_rules` replicates TCKDB's level-of-theory hash (aliases, folded dispersion,
  `core_treatment` only when stated). Run `test_hash_matches_the_backend_payload_rules` (and, in the TCKDB environment, `test_hash_matches_the_real_backend_function`; see `tckdb_core/README.md`) with `TCKDB_BACKEND_PATH` set to a TCKDB
  `backend/` (CI does).
- Not in this release: the `composite` calculation type (waits on an ARC export), transport, core extraction.

## 0.9.0

The adapter consumes ARC output schema 1.3 at PR #1059 head `ebc88ec8`. Documents from the earlier 1.3 draft
take the previous paths unchanged.

- **Reaction species labels.** `reactant_species_labels` / `product_species_labels` (one entry per occurrence) are the
  order and repeats of `reactant_keys` / `product_keys` on both routes; a contradiction with `atom_map_*_labels` or
  `reactant_labels` refuses the reaction (`reaction_species_labels_contradicted`).
- **Reaction `atom_map` on both routes.** `ts_atom_map` becomes TCKDB's `atom_map` (`atom_to_ts` per participant,
  `source: inferred`, a `note` naming ARC's method and its symmetry-equivalent-atom convention) after checking block
  lengths, TS index coverage, element conservation, `atom_map` consistency and consistency with the IRC participant
  mapping that is sent; otherwise no map and `reaction_ts_atom_map_not_sent` (with `ts_atom_map_unavailable_reason`
  in the context when ARC states `null`).
- **`nmd_forced`.** A forced NMD pass gives `mode_displacement_agrees: False` (the check ran and failed), never `True`; an index stated beside it is
  reported (`ts_nmd_forced_contradicts_reaction_coordinate_index`) and not used as the NMD designation.
- **Conformer programs.** `conformer_ess_software` / `conformer_ess_version` give each screened conformer's program
  and banner; conformers with a null program are still not filed. `conformer_energies` null entries send no energy.
- **`bac_type`.** `bond_corrections_applied: true` with a null header `bac_type` warns (`bac_type_not_stated`); the
  BAC scheme is still built from the record's `energy_corrections`. `reference_pressure_bar` is still omitted, never
  defaulted, when `standard_state_pressure_pa` is null (ARC 1.3 final always states 101325).

## 0.8.1

The adapter sends the two other TS validation evidence kinds tckdb-schemas 0.64 added, only from
what ARC states:

- **`imaginary_mode`** (both TS routes) from `ts_checks['freq']`: `imaginary_frequency_count` and
  `imaginary_frequency_cm1` (the designated reaction-coordinate mode, written negative) are read from the TS
  frequency result sent in the same upload, so TCKDB's cross-check against it passes; `mode_displacement_agrees`
  is `True` only when ARC 1.3 states `reaction_coordinate_mode_index`, `False` only for a failed
  `ts_checks['NMD']` with no index, otherwise omitted. A passing record with several imaginary modes and no
  designated coordinate is not sent (`ts_imaginary_mode_evidence_not_sent`).
- **`energy_ordering`** (reaction bundle only; the standalone route refuses it) from `ts_checks['e_elect']`,
  with every participant's own `sp_energy_hartree` cited to its `sp` calculation (`reactant:N` / `product:N` in
  the declared order, a repeated species repeated). Electronic energies only: `e0` is never sent. A `True`
  verdict the stated numbers contradict (ARC's 1 kJ/mol margin), or a participant with no usable energy or `sp`,
  leaves the record out (`ts_energy_ordering_evidence_not_sent`). ARC leaves `e_elect` unset when its `E0`
  check passed, and then nothing is sent.

## 0.8.0

The adapter reads output.yml 1.3 (ARC PR #1059): each record's `levels` (the level of its
opt, freq, sp, composite and IRC jobs, also under `adaptive_levels`) replaces the `restart.yml`
replay, programs come from `ess_software` (including `composite`, `irc` and the xtb GSM) and
each rotor scan's own program, the header `gsm_level` files the GSM path search again, screened
conformers are filed at their `conformer_levels` (force-field geometries are not calculations),
IRC endpoint species are skipped by `irc_endpoint_of`, a composite run's job is filed as the
primary calculation at the composite level and linked under the `composite` role, stated
isotopes ride on the geometries (a species whose stated isotopes contradict its SMILES is
refused), and the observed `*_route` lines become `parameters`. Pre-1.3 output behaves as
before.

The adapter reads ARC output schema 1.3's reaction atom map and IRC participant mapping.
`irc_participant_mapping` becomes the IRC validation evidence's `reactant_participant_mapping`
/ `product_participant_mapping` (1-based TS atoms, participants matched by label and
occurrence), and only when ARC states that the IRC endpoints follow the TS atom order and the
sides are distinguishable; otherwise neither side is sent
(`ts_irc_participant_mapping_not_sent`). ARC's reactant-to-product `atom_map` is **not** sent:
TCKDB's `atom_map` is participant atom to TS atom and the bc731fb4 draft of ARC 1.3 states no relation to the TS atom
order, so the adapter reports `reaction_atom_map_ts_order_not_stated` for a document without `ts_atom_map` (TCKDB
then warns `reaction_atom_map_absent`); adapter 0.9.0 sends it from ARC's `ts_atom_map`.

The adapter reads ARC output schema 1.3 (the keys are read from 1.3 documents
only, and a document before 1.3 gives the payload it gave before):

- A partial Petersson BAC is sent with the bonds Arkane applied (`components`, which must
  sum to the total) and its skipped bonds in the correction's note, instead of being omitted.
- `statmech_treatment` is `statmech.arkane_treatment`, the treatment Arkane applied, with no
  Hessian gate; a torsion's `treatment` is sent as recorded and a null one sends the torsion
  without a treatment. The rigid-rotor kind (now including symmetric and spherical tops) is
  sent as stated.
- The kinetics `note` carries Arkane's comment, the `ts_validation` text and the kinetics run's
  atom-correction switch; the reaction's `reversible` is sent as stated.
- A correction scheme names the RMG-database table Arkane loaded. A first draft did that with a
  `RMG-database` `workflow_tool_release` (commit, package version or `sha256:`); that stand-in is
  replaced by `scheme.data_revision` (tckdb-schemas 0.62).
- A TS's modes are its frequencies in ESS order and its reaction-coordinate mode is the one ARC's
  normal mode displacement check validated; with none stated, TCKDB's 50 cm-1 noise floor picks the one
  mode above it (the others `unassigned`), and two or more above it refuse the record.
- `sp_t1_diagnostic` becomes the sp calculation's `wavefunction_diagnostic`. The dipole moment
  and polarizability have no home on these routes (TCKDB holds them only on the standalone
  transport record), and the E0 switches gate nothing since no E0 is deposited.

Upgrading changes the payload hash, and so the idempotency key, of 1.3 uploads that gain any of
the above.

The adapter uses what tckdb-schemas 0.59 to 0.64 added:

- **Atoms (0.59).** A conformer whose own XYZ has exactly one atom sends `type: "sp"` as its
  primary calculation (computed-species, computed-reaction and conformer routes): the atom's own
  log, level, program and energy (`sp_energy_hartree`, else the `opt_final_energy_hartree` ARC
  parsed from the same one log), no `opt`, no `converged` (an sp result has none), no placeholder, no
  warning, no `freq`, no coarse opt, no rotor scans and no alternative conformers. Thermo and statmech
  link that one sp once (`thermo_role_duplicate` / `statmech_role_duplicate` need two on one
  geometry). An atom with neither energy is refused (`ValueError`), never relabelled as an opt. The
  placeholder opt stays only for a molecule with no opt job (`primary_opt_placeholder_no_opt_job`) or
  a composite run (`composite_geometry_level_not_stated`).
- **Scheme provenance (0.62).** `scheme.data_revision` is the RMG-database revision Arkane read:
  the git commit for `path_kind: git`, else the package version, else the `data.py` SHA-256 as a
  plain hex string. It joins the scheme's identity, so the Arkane build (still stamped as
  `scheme.workflow_tool_release`, provenance only) stops splitting schemes. The `RMG-database`
  tool-release identity set of the first draft was never deposited (it existed only on an
  unmerged branch), so no deposited scheme has it; a scheme deposited with a revision never
  matches one without, so the first upload of each table under 0.8.0 makes a new row. An
  `atom_energy` scheme with `atom_params` also sends `atom_params_applied_as` as ARC states it
  (`subtracted`); the scheme note keeps only Arkane's per-atom addend (`atom_hf - atom_thermal`),
  which that field does not cover.
- **Kinetics (0.63).** ARC's `A` is sent as it is with `t0_k` = ARC's `T0_k` (omitted when 1 K,
  the contract default), instead of `A / T0**n`; a `T0_k` that is null, invalid or outside
  `0 < t0_k <= 10000` omits `a`/`a_units`. `tunneling_application`, `interpretation_assignments`
  and `network_kinetics_ref` are not sent: every reference in them is a record deposited earlier,
  and the bundle cannot cite the transition state or statmech it is itself creating. The tunnelling
  label is still ARC's `tunneling` as `tunneling_model`.
- **Rejected rotors (0.61).** ARC's rejected rotors go out as torsions with `invalidated_reason` on the bundle
  routes too (they were conformer-route only).
- **Standalone transition-state route (0.64).** Rotor scans (with `scan_result`) and
  `applied_energy_corrections` (without source keys) are now sent; its `atom_map` slot was left unset
  until ARC stated `ts_atom_map` (adapter 0.9.0 sends it).
- Not built, no source in ARC or a decision needed: statmech `electronic_levels` (ARC 1.3 exports no
  electronic level, term or spin-orbit data), bundle `transport` (ARC's dipole and polarizability keys
  could feed it) and TS `statmech` on the reaction bundle. An IRC result whose
  direction ARC does not state is still withheld although 0.64 made `direction` and the flags optional.

## 0.6.7

- A species on both sides of a reaction (or repeated on one side) is deposited once; the
  computed-reaction bundle repeats its key in `reactant_keys` / `product_keys`, and the
  kinetics `reactant_energy` / `product_energy` links point at its sp once per role.
- A single-atom species was uploaded with a placeholder primary opt and a
  `monatomic_species_primary_opt_placeholder` warning (TCKDB#600). Superseded later in 0.8.0: an atom
  now sends its real `sp` as its primary calculation (TCKDB#610, tckdb-schemas 0.59), with no
  placeholder and no warning.
- Each calculation's artifact batch is sent through `TCKDBClient.request_json`, so the
  artifact sidecar (and `ArtifactUploadOutcome.warnings`) carries the server's warnings,
  the HTTP status, the request id and the replay flag.
- A rotor scan calculation carries its log (`rotor_scans[].source_log`) as an
  `output_log` artifact, and a reaction species' conformer carries `label`.
- IRC endpoint species (`IRC_<ts>_<n>`) are skipped, with `irc_endpoint_species_skipped`,
  when the project's `restart.yml` records them (`irc_label`); without `restart.yml` they
  are uploaded as before.
- The adapter no longer reads keys ARC does not export (`unmapped_smiles`,
  `reactions[].reversible`, `kinetics.degeneracy`, `kinetics.note`, `irc_final_settings`,
  `sp_spin_diagnostic.note`, `electronic_energy_hartree`), except that for output.yml 1.0
  documents `thermo.cp_data` and the atom-energy `parameter_table` are still read.

Upgrading changes the payload hash, and so the idempotency key, of reaction uploads that
had a repeated species or gained conformer labels or scan logs.

## 0.6.6

Thermo and statmech declare `energy_level_of_theory`, the linked sp's own
level with its `spin_treatment` (TCKDB hashes it into the level identity, so ARC's bare level
would be refused); it is omitted when ARC's energy level is not that calculation's (a composite
method, an unattributable adaptive level). ARC's `wavefunction_stability` becomes the
primary opt's `scf_stability`, the job ARC's analysis tests (not when ARC re-optimized at a
derived reference; `stable` only for ARC's `stable` verdict; never on freq/sp). Conformer mode now also sends `statmech` and
`applied_energy_corrections`, built by the computed-species builders, with ARC's rejected
rotors as torsions carrying `invalidated_reason`; it sends no rotor scan (the route accepts
only freq and sp as additional calculations).

## 0.6.5

The adapter targets tckdb-schemas 0.54 (no wire model change; only the
bundles dry-run route, which the adapter never calls, gained rules) and states
Arkane's full atom-energy convention in the `atom_energy` scheme note: the atomic
energies are subtracted and each atom's gas-phase formation enthalpy less its
thermal correction is added.

## 0.6.4

The adapter states a level only when ARC's output or the project's `restart.yml`
supports it. Under `adaptive_levels`, `restart.yml` (adaptive spec, each species'
`adaptive_lot_n_heavy`, rotor scan types) lets the adapter replay ARC's per-species
choice exactly (`opt`, `freq`, `sp`, `composite`, `irc`, `scan`, `directed_scan`, matched
case-sensitively); a species' attributed sp/composite level is the energy level its
enthalpy is checked against, so formation enthalpies survive. When `restart.yml` or the
species entry is missing, or only `input.yml` is available, an `opt` in the spec refuses
the upload, named `sp`/`freq`/`scan`/`irc` calculations are omitted
(`<kind>_level_adaptive_not_attributable`) and a named `sp` or composite level strips
thermo enthalpy (`enthalpy_adaptive_levels_unverifiable`); with only `output.yml` an
adaptive run cannot be detected. The IRC uses the `irc_level` `restart.yml` records;
without one it keeps `opt_level` under `irc_level_assumed_opt_level` (the run's settings
default, unreadable here, assumed equal). Screened alternative conformers are filed at
`restart.yml`'s `conformer_opt_level` (with its program) when `conf_opt` ran and the spec
does not name it, otherwise omitted (`conformer_level_not_stated`). The TS-guess path
search is filed only for NEB, at `neb_level` and the observed NEB program
(`ess_software.neb`); a GSM guess, NEB without `neb_level` or without an observed program
is omitted (`ts_guess_level_not_stated`, `ts_guess_software_not_stated`).
`ts_checks['IRC']`, ARC's IRC verdict, becomes TS `validation_evidence` (`passed` is that
verdict, never `irc_converged`; the rationale is `ARC ts_checks['IRC'] = <verdict>`); no
verdict sends none. Upgrading changes the payload hash, and so the idempotency key, of TS
uploads with a GSM guess and of species uploads whose level attribution changed.

## 0.6.3

The adapter sends corrections and a statmech treatment only as far as the
evidence goes. A Petersson `bac_total` is sent only with a complete bond
decomposition: TCKDB refuses the whole upload for one with no `bond`
component (`bac_total_requires_components`, except on a monatomic species) and
does not check that a partial decomposition sums to the total. ARC drops every
component when any bond lacks a parameter, so a componentless or partly null
Petersson BAC is omitted, with a `bac_correction_omitted_components_incomplete`
warning (reason `no_components`, `component_unusable` or `no_bond_component`);
the AEC correction and the rest of the payload are still sent. This holds for
species, reaction participants and the reaction's TS block. A rotor-aware `statmech_treatment`
and each torsion's `treatment_kind` are sent only when a freq Hessian was found
for the species (Arkane ignores every rotor without a force-constant matrix);
otherwise they are omitted with `statmech_treatment_not_stated` (torsions are
still sent; plain `rrho` needs no Hessian). An omitted BAC is noted on the
thermo record according to `thermo.bond_corrections_applied` (true: Arkane
applied it, not deposited; false: no note; absent: the note states only that
ARC exported a Petersson BAC total that is not deposited). Atom-energy,
Petersson and Melius schemes carry Arkane as `workflow_tool_release`. Its
`git_commit` is the RMG-Py commit Arkane ran from; the correction tables come
from RMG-database, whose commit output 1.3 records (since adapter 0.8.0,
it travels as `scheme.data_revision`, which stops the Arkane build from splitting
schemes). Before output 1.3 a database-only
revision of a table value for the same key still conflicts with the stored
scheme (a parameter-conflict 422), and each new RMG-Py commit makes a new
scheme row even when the tables are unchanged. The first 0.6.3 upload of an
already-deposited scheme also creates a new row (identity now includes the
workflow tool; the older null-tool row remains). An `atom_energy` scheme now carries
`scheme.atom_params` from ARC's `reference_atom_energies` (Arkane's per-element
atomic energies) with `scheme.units` set to that table's own unit; a table with
a missing or unrecognized unit, or unusable values, is not sent.

## 0.6.1

The adapter passes ARC's software provenance through. Each calculation's
ESS banner (`ess_versions`, e.g. `Gaussian 16, Revision C.02`) is split into
`version` `16` and `revision` `C.02` by TCKDB's own shared
`SoftwareReleaseRef` normaliser; a banner naming another program, or of any
other shape, is sent unchanged. Computed-species thermo and statmech name
Arkane (`arkane_version`, `arkane_git_commit`) as their `software_release`.
Each energy-correction scheme names the program its parameters come from as
`scheme.software`: the `software='<name>'` in the record's
`matched_arkane_key` (Arkane's database entry, e.g. `gaussian`), with no
release. It is omitted when the key is absent or names no software, and
omitted with an `energy_correction_scheme_software_conflict` warning when it
disagrees with the correction level's software. Literature is not sent: ARC
records no citation.

Upgrading to 0.6.1 changes the payload hash, and so the idempotency key, of
every ARC upload whose ESS banner is composite (e.g. `Gaussian 16, Revision
C.02`), and of every species upload that now carries Arkane or scheme
software. Replaying a sidecar written before 0.6.1 posts under a new key, so
TCKDB records it as a new deposit rather than replaying the old one.

## 0.6.0

The adapter conforms to the tckdb-schemas 0.52 producer contract where ARC does not state a value:
it omits `reference_pressure_bar` without a recorded pressure, omits
`path_search_result.converged` (ARC's TS-guess `success` means only that the
output file exists), GSM `is_climbing_image` (an NEB-CI flag the contract
says string methods ignore), `freq_scale_factor.scale_kind` and
`species_entry.electronic_state_kind` (ARC states neither; TCKDB applies its
own default), and refuses an IRC result when any log's direction is unstated
(`irc_direction_not_stated`, action `irc_result_omitted`; the `irc`
calculation is still sent) instead of claiming `both`. A species or TS record
without a usable integer `charge` or `multiplicity` is refused (the upload is
not built; the sweep reports it) instead of becoming charge 0 or a singlet.
ARC does not export `irc_level`, so the IRC calculation keeps `opt_level` and
every one reports `irc_level_assumed_opt_level`. Kinetics `tunneling_model` is
sent as the contract's lowercase token (`eckart`, unknown methods `other`),
the value TCKDB stored anyway.
