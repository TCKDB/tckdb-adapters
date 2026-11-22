# What the ARC adapter sends to TCKDB

This page describes the current mapping from an ARC project to TCKDB payloads: what is
read, what each value becomes, and what is left out. The history of how it got here is in
[`adapters/tckdb_arc/CHANGELOG.md`](../../adapters/tckdb_arc/CHANGELOG.md). The codes in `this form` that
end a rule are warning codes; [`WARNING_CODES.md`](WARNING_CODES.md) lists them all.

The contract itself is TCKDB's. Read it with `python -m tckdb_schemas.contract --print`.
This page records only how ARC's output is fitted to it.

## Rules the mapping follows

1. **A value ARC does not state is not sent.** The field is omitted and the omission is
   reported in the sidecar under a warning code. The exceptions are listed below.
2. **A record that cannot be built correctly is refused.** A species without a usable
   integer `charge` or `multiplicity` raises an error and that upload is not built.
3. **An interpretation is marked as one.** Where the adapter sends its reading of ARC's
   documented behaviour, the payload says so in a `note` or a `claim_origin`, and the
   sidecar carries a warning. The exceptions below are marked less fully.
4. **A minimum with an imaginary frequency is refused.** ARC rejects such species and the
   adapter does the same. TCKDB's tolerance for soft modes is not applied.

### Exceptions to rules 1 and 3

Three values are sent although the output being read does not state them. Each was a
deliberate decision, and each is described in full in its own section.

| Value | When | How it is marked |
|---|---|---|
| `enthalpy_reference_kind: formation_298k` | Output before 1.2, which records no atom-correction switch, when the heuristic checks under "Thermo" pass. | Not marked in the payload or the sidecar. A stripped enthalpy is reported; a kept one is not. |
| `reversible: true` on a standalone transition state | The reaction states no `reversible` (output before 1.3, or a null). | Not marked. The route requires the field. |
| The opt level on an IRC calculation | The output states no IRC level. | `irc_level_assumed_opt_level` in the sidecar; nothing in the payload. |

## What is read

| File | Use |
|---|---|
| `output/output.yml` | The source of every record. Schema versions 1.0 to 1.3 are read; keys added in a later version are read only from documents of that version. |
| `output/parser_evidence.json` | Hessians, IRC trajectories and GSM paths. The older `tckdb_evidence.json` is also read. |
| `restart.yml` | Only for output before 1.3: adaptive levels, the IRC level, the conformer level and IRC endpoint labels, none of which that output records. |
| `input.yml` | The `tckdb` block, and detection of `adaptive_levels` for output before 1.3. |
| Calculation logs and input decks | Uploaded as artifacts when artifact upload is on. Parsed only as a fallback when no evidence file exists and ARC is importable. |
| The deck text in `output.yml` 1.3 | `ess_input_options` (per job), `irc_log_input_options`, `conformer_input_options` and `conformer_energy_input_options` are read for the typed level qualifiers (see "Levels of theory"). The log echo of a job (`*_route`, `*_blocks`, `irc_log_routes`, `irc_log_blocks`) is read only to check that a qualifier the deck states is not contradicted. |

The adapter does not read keys that ARC's `output.yml` writer never writes. A key added to
a record by hand is ignored.

## Species and geometries

- `label`, `smiles`, `charge` and `multiplicity` become the species entry. Charge and
  multiplicity must be stated integers.
- `electronic_state_kind` is not sent. ARC computes the multiplicity it was asked for,
  which need not be the ground state, so TCKDB applies its own default.
- The record's `xyz` is the conformer geometry. Stated isotopes (`xyz_isotopes`, output
  1.3) travel on the geometry. A species whose stated isotopes contradict its SMILES is
  refused.
- A species that is an IRC endpoint of a transition state is not uploaded
  (`irc_endpoint_species_skipped`). Output 1.3 marks these with `irc_endpoint_of`; older
  projects are recognised through `restart.yml`.
- An atom's primary calculation is its single point: its own log, level, program and
  energy. It gets no `opt`, no `freq`, no rotor scans and no alternative conformers. An
  atom with no usable energy is refused.
- A species on both sides of a reaction, or repeated on one side, is deposited once. The
  reaction bundle repeats its key in `reactant_keys` or `product_keys`.

## Calculations

Each ARC job becomes a TCKDB calculation at the level and program ARC states for it.

| ARC | TCKDB | Notes |
|---|---|---|
| opt job | primary `opt` calculation with `opt_result` | A molecule with an sp or freq log and no opt job gets a placeholder opt at the header level (`primary_opt_placeholder_no_opt_job`). |
| freq job | `freq` calculation, frequencies, Hessian | The Hessian keeps its own coordinate frame and carries `parser: {name, version}`. `freq_hessian_method` is passed through when it is `analytic`, `finite_difference_gradient` or `finite_difference_energy`, and never derived from the deck. |
| sp job | `sp` calculation | When ARC reused the opt output for the energy (`sp_level` null or equal to `opt_level`), the sp is marked `reused_result`. `sp_t1_diagnostic` becomes `wavefunction_diagnostic`. |
| composite job | the primary calculation at the composite level, linked under the `composite` role | |
| rotor scans | `scan` calculations with `scan_result` | Each scan needs its own stated level and program; a scan without them is reported and not relabelled with the opt level. The scan's log is its `output_log` artifact. |
| IRC jobs | `irc` calculation with `irc_result` | See the transition-state section. |
| TS guess search | `path_search` calculation with `path_search_result` | NEB at `neb_level`, GSM at the header `gsm_level` (output 1.3). Without a stated level or program it is omitted (`ts_guess_level_not_stated`, `ts_guess_software_not_stated`). The points are described under "Path-search points" below. |
| screened conformers | bare `opt` calculations marked as screened-conformer anchors | Filed at `conformer_levels` with each conformer's own program. A conformer with no stated level or program is omitted (`conformer_level_not_stated`). Force-field geometries are not calculations. |

Other per-calculation values:

- **Software.** The program comes from `ess_software` for that job. Its banner in
  `ess_versions` (for example `Gaussian 16, Revision C.02`) is split into `version` and
  `revision` by TCKDB's own `SoftwareReleaseRef` normaliser. A banner of any other shape is
  sent unchanged. No job borrows another program's version.
- **SCF stability.** `wavefunction_stability` becomes `scf_stability` on the primary opt,
  the job ARC's analysis tests. It is never set on freq or sp.
- **Deck and log echo.** Where the record also states what the job's log echoed
  (`*_route` and `*_blocks`, `irc_log_routes` and `irc_log_blocks`), that text is parsed like
  the deck and can only veto: a qualifier the deck states and the echo states differently is
  left out (`level_qualifiers_conflicting`). A qualifier only the echo states is not sent.
- **Route lines.** The observed `*_route` lines (output 1.3) become calculation
  `parameters`. For jobs that ran in Gaussian the route is compared with the stated level;
  a calculation whose route names another method or basis is omitted
  (`level_contradicted_by_route`). ORCA keyword lines are not compared, because their
  auxiliary-basis tokens look like a `method/basis` pair.
- **Frequency scale factor.** The factor is sent; `scale_kind` is not, because ARC's table
  mixes fundamental and harmonic factors without saying which.
- **Input geometries.** A freq or sp whose declared `input_geometries` are not its
  conformer's own is sent without them (`calculation_input_geometry_not_declared`).
- **Isotopes on Hessians and path points.** A substituted species' Hessian and its scan or
  IRC points are labelled with the record's isotopes when the frame has the record's
  element sequence in order and the program is Gaussian or ORCA. Any other point has its
  geometry left out (`geometry_isotopes_not_stated`).

### Path-search points

A path search carries its whole path where the source holds it. Relative energies are
referenced to the lowest energy on the path.

**ORCA NEB.** Every image of the log's band `PATH SUMMARY` becomes a point
(`tckdb_arc.orca_neb`):

- `path_coordinate` from `Dist.(Ang.)`, with `path_coordinate_unit: angstrom`, the energy,
  and `max_force` and `rms_force` from `max(|Fp|)` and `RMS(Fp)` in Eh/Bohr. The force is the
  one perpendicular to the path, and the total force at the two end points.
- The climbing image is marked `is_climbing_image` and named by `climbing_image_index`. It
  is the only image with a geometry, taken with its full-precision energy from the log's
  saddle-point block. The other images' geometries are not in the log.
- `converged: true` is sent when ORCA printed `THE NEB OPTIMIZATION HAS CONVERGED`.
  Otherwise the field is omitted. ARC's own `success` flag is never used for it, because
  it means only that an output file exists.
- The summary is not read when it does not end the way ORCA ends it, or when its image
  count differs from the count the run states (or the input's `NImages` plus the two end
  points). The search then keeps a single point, the TS guess from `opt_input_xyz`.
- No image is marked as the TS guess. ARC takes its guess from the log's last coordinate
  block, which for its NEB-TS runs is the saddle point ORCA optimized from the climbing
  image and is not an image. That geometry (`opt_input_xyz`) is sent as the calculation's
  `final` output geometry, and the result's `note` says so. The saddle point's energy and
  forces are not sent; the result has no field for a point that is not an image.

**xtb GSM.** The points are the stringfile frames in ARC's parser evidence. A point
carries an xTB energy and gradient norm only where the evidence matches a node evaluation
to that frame's geometry. A frame with no match has no energy. A point's `path_coordinate`
is its cumulative displacement along the string in Angstrom, sent with
`path_coordinate_unit: angstrom`. Values are never attached
by node number, the stringfile's all-zero energy column is not read as energies, and
without an evidence file the archived node values are left out. GSM `converged` and
`is_climbing_image` are not sent: ARC keeps no GSM output that states convergence, and the
contract defines the climbing-image flag for NEB.

## Levels of theory

- For output 1.3, each record's `levels` object gives the level of its opt, freq, sp,
  composite and IRC jobs, including under `adaptive_levels`.
- For earlier output, the header levels (`opt_level`, `freq_level`, `sp_level`,
  `composite_method`) apply. A null `freq_level` is the opt level, as in ARC itself. Under
  `adaptive_levels` the adapter replays ARC's per-species choice from `restart.yml`. When
  that file or the species entry is missing, calculations whose level cannot be attributed
  are omitted (`<kind>_level_adaptive_not_attributable`), an unattributable opt refuses the
  upload, and thermo enthalpy is stripped (`enthalpy_adaptive_levels_unverifiable`).
- A level is ARC's `method`, `basis`, `auxiliary_basis` (TCKDB `aux_basis`), `cabs`
  (`cabs_basis`), `dispersion`, `solvent` and `solvation_method` (`solvent_model`), with
  `spin_treatment` from the job's `scf_reference`. The program belongs to the calculation's
  `software_release` and is not part of the level.
- No level carries `keywords`; TCKDB removed that field. The typed qualifiers that replaced
  it (`pno_truncation`, `ri_approximation`, `relativistic_hamiltonian`, `core_treatment`) are
  stated on a calculation's level, and only there, when the input-deck text ARC recorded for
  that calculation's job carries the token:

    - The deck is `ess_input_options` at the job key the calculation's program is read from
      (so a composite run's calculations read the composite job's deck, and an sp whose energy
      came from the opt log reads the opt job's), `irc_log_input_options` for an IRC
      calculation, `conformer_input_options[i]` for a screened conformer's opt and
      `conformer_energy_input_options[i]` for its single point. The deck is parsed as the
      program of the calculation (`software_release.name`). A record without a deck, or
      without the key, states none: a missing key means the same as null.
    - A calculation ARC records no deck for states none, and no job's qualifiers are copied
      onto another level: the coarse stage of an optimization (the `opt` slot holds the fine
      opt's deck, not the coarse stage's), rotor scans, path searches, and an opt that is a
      placeholder for a job ARC did not run. A level that is no job's (a frequency scale
      factor's level, a correction scheme's level, the header levels) states none either. ARC's
      level `args` are never read.
    - An IRC calculation states a qualifier only when every IRC log has a recorded deck and
      all the decks state the same qualifiers; decks that differ state none
      (`level_qualifiers_conflicting`).
    - A value is read only from a token the deck states, as the lower-case enum value. A token
      ARC writes by default (`normalpno` for a DLPNO method) is read like any other. Absence is
      never a value, a program default is never inferred, and a method name (`RI-MP2`,
      `DLPNO-CCSD(T)`) states nothing. A field the deck states with two values is left out
      (`level_qualifiers_conflicting`); the others are kept. A negation or a value the adapter
      does not state counts as a value for this purpose only: `NoCOSX` beside `RIJCOSX`,
      `NoRI` or bare `RI` beside another `ri_approximation` token, Gaussian `NoDKH` or
      `NonRelativistic` beside a `DKH` option. None of them is ever sent.
    - A key beginning with `TCut` (`TCutPNO`, `TCutPairs`, `TCutDOij`, `TCutPNOSingles`, ...)
      in an ORCA `%mdci` or `%mp2` block is a numeric threshold that overrides the PNO preset,
      so a stated `pno_truncation` is not sent; the finding is reported under
      `level_qualifiers_conflicting` with the reason "a numeric threshold overrides the preset".
    - A deck is not read when it does not have the shape of its program (an ORCA deck starts
      with `!`, `%` or `$`, a Gaussian one with the `#` route), when it holds more than one job
      (`$new_job`, `--Link1--`) or when a Gaussian route has a `!` comment
      (`level_qualifiers_deck_not_read`). Programs other than ORCA and Gaussian are not read and
      nothing is reported for them.
    - Every token below is also read by TCKDB's own ORCA and Gaussian parameter parsers, and a
      test compares the two on the same deck text, so the adapter never states more than TCKDB
      reads from the same deck. The Gaussian route is split into tokens the way TCKDB splits
      it (on a space outside parentheses). The code is
      `tckdb_core/tckdb_core/deck_qualifiers.py`.

    | Program | Token | Field = value | Source |
    |---|---|---|---|
    | ORCA | `TightPNO`, `NormalPNO`, `LoosePNO` | `pno_truncation` = `tight`, `normal`, `loose` | ORCA 6.1 manual, "Local correlation (DLPNO)": "specifying TightPNO, NormalPNO or LoosePNO keywords" |
    | ORCA | `RIJCOSX`, `RIJK`, `RIJONX` | `ri_approximation` = the same, lower case | ORCA 6 manual, the RIJCOSX ("Basic Usage"), RIJK and RIJONX keywords |
    | ORCA | `DKH2`, `DKH` | `relativistic_hamiltonian` = `dkh2` | ORCA 6.1 manual, relativistic options: "DKH2, DKH: Activate the second-order DKH Hamiltonian". The Order condition below applies to both |
    | ORCA | `ZORA`, `IORA`, `X2C` | `relativistic_hamiltonian` = `zora`, `iora`, `x2c` | ORCA 6.1 manual, relativistic options (the `ZORA`, `IORA` and `X2C` keywords) |
    | ORCA | `%rel Method DKH`, `ZORA`, `IORA`, `X2C` | `relativistic_hamiltonian` as above | ORCA 6 manual, the `%rel` block (`Method`, `Order`) |
    | ORCA | `FrozenCore`, `NoFrozenCore` | `core_treatment` = `frozen_core`, `all_electron` | ORCA 6 manual, "Frozen Core Options" |
    | ORCA | `%method FrozenCore FC_EWIN`, `FC_ELECTRONS`, `FC_NONE` | `core_treatment` = `window`, `frozen_core`, `all_electron` | ORCA 6 manual, "Frozen Core Options"; ORCA 5.0.4 manual, the same section |
    | Gaussian | `Int(egral)=DKH`, `=DKH2`, `=DouglasKrollHess` (also `=(DKH2,...)`, `Int(DKH2)`) | `relativistic_hamiltonian` = `dkh2` | Gaussian 09 manual, Integral keyword, "Relativistic calculations": "DKH: second order ... DKH2 and DouglasKrollHess are synonyms" |
    | Gaussian | `Int(egral)=DKH0`, `DKHSO`, `RESC` (same option-list forms) | `relativistic_hamiltonian` = `dkh0`, `dkhso`, `resc` | Gaussian 09 manual, Integral keyword, "Relativistic calculations" (the manual of Gaussian 16 is not separately cited) |
    | Gaussian | `Full`, `FC` (or `FrozenCore`), `Window` as an option of a correlated method, as in `CCSD(T,Full)/cc-pVTZ`, `MP2(FC)` | `core_treatment` = `all_electron`, `frozen_core`, `window` | Gaussian manual, the MP2 and coupled-cluster keywords (frozen-core options); not `Pop=(Full)` |

    `DKH` and `DKH2` state `dkh2` only when every `Order` of the deck's `%rel` block is 2 or
    none is given; with another order the field is not sent.

    Not sent: bare ORCA `RI` alone (TCKDB reads it as `rij`; it only contradicts another
    `ri_approximation` token), `NoCOSX` alone, `DKH1`, `DLU-X2C`, numeric PNO thresholds,
    explicit negations (`none`), Gaussian density fitting, and every spelling outside the table.
- A method containing `//` names two levels. It is never sent, and a calculation at such a
  level is not built (`level_method_is_compound`).
- An Arkane level that is the name of a correction table (`cbs-qb3-paraskevas`) is sent as
  its method stem, with the table name as the correction scheme's `name`
  (`correction_table_method_split`).
- The IRC calculation uses the stated IRC level. For output that states none it keeps the
  opt level and reports `irc_level_assumed_opt_level`.
- Thermo, statmech and kinetics blocks declare `energy_level_of_theory` only when it is the
  level of the linked energy calculation. For kinetics the linked energies must agree on
  every identity field. Spin is judged per participant, as TCKDB does: closed-shell links
  never constrain it, and the open-shell links must share one reference
  (`kinetics_energy_level_spin_mixed` otherwise). A one-electron species computed at
  Hartree-Fock under a correlated level is accepted (`one_electron_hf_substitution`).
- A declared `energy_level_of_theory` is the linked calculation's level as sent, qualifiers
  included, because TCKDB requires it to be that level's row. The comparison with ARC's
  stated (requested) energy level treats a qualifier stated on one side only as agreeing, and
  two different stated values as two levels. The linked energies of a kinetics block are
  compared with one another strictly, as TCKDB does, so links that differ only in a qualifier
  (a one-electron Hartree-Fock atom whose deck has no `normalpno`, beside DLPNO participants
  whose decks do) declare nothing and say so (`kinetics_energy_level_qualifiers_differ`).
  The adapter reports nothing about a qualifier stated on a calculation and not on a requested
  (stated) level.

## Thermo

`H298`, `S298`, the NASA polynomials and the Cp/H/S/G points become the thermo block, with
source-calculation links to the species' opt, freq, sp and composite calculations.
Computed-species thermo and statmech name Arkane (`arkane_version`, `arkane_git_commit`)
as their `software_release`. The block is assembled by the adapter and never through
`tckdb_client.builders`, whose defaults would declare an enthalpy reference on ARC's
behalf.

### Reference pressure

Blocks with entropy content carry `reference_pressure_bar` only when ARC recorded
`thermo.standard_state_pressure_pa` as a number between 0.5 and 2 bar. Otherwise the field
is omitted, TCKDB stores the pressure as not stated, and the sidecar reports
`thermo_reference_pressure_not_stated`. The rest of the block is still sent.

### When an enthalpy is declared a formation enthalpy

Blocks with enthalpy content declare `enthalpy_reference_kind: formation_298k`, because
Arkane's corrected H298 and NASA fit are formation enthalpies at 298.15 K. That holds only
if Arkane applied atom-energy corrections at the species' own energy level. When it cannot
be shown, the enthalpy content (H298, the NASA fit, point H and G) is stripped and S298 and
the S and Cp points are kept. Each strip is reported with action `thermo_enthalpy_omitted`,
or `thermo_omitted` when nothing is left.

The finished block is also checked with TCKDB's own `enthalpy_reference_error`. A block
that rule refuses is omitted whole, with the rule's code, and the rest of the upload is
sent. A block with only Cp content carries neither `enthalpy_reference_kind` nor
`reference_pressure_bar`.

Output 1.2 and later record whether Arkane applied the corrections
(`thermo.atom_corrections_applied`) and at which level (`thermo.atom_corrections_level`).
The enthalpy is stripped when:

- the switch was off (`enthalpy_atom_corrections_not_applied`);
- the corrections level is not the species' energy level
  (`enthalpy_atom_corrections_level_mismatch`). The two are compared by method, with its
  dispersion folded in, and basis, normalised as ARC's own level matching does: case,
  hyphens, spaces and a trailing refit year are ignored, `ccsd(t)` and `ccsdt` stay
  distinct, and the program is ignored. One exception: a one-electron species at
  Hartree-Fock under a wavefunction or multireference corrections level that is otherwise
  identical keeps its enthalpy (`one_electron_hf_substitution`);
- either level sets `dispersion` or `solvation_method`, which ARC's atom-energy matching
  ignores, so a match proves nothing (`enthalpy_atom_corrections_level_unverifiable`);
- a value is not finite (`enthalpy_not_finite`), or H298, a point H or the NASA H at
  298.15 K lies outside ±2.0e4 kJ/mol (`enthalpy_not_formation_magnitude`).

Output before 1.2, and thermo that Arkane loaded from its own YAML file, record no switch.
Declaring `formation_298k` for them is a deliberate interpretation: Arkane's corrected H298
is a formation enthalpy by construction. It is made only after the non-finite and
magnitude checks above and these three, applied in order:

- the header `arkane_level_of_theory`, when it names a method, must be the energy level
  (`enthalpy_atom_corrections_level_mismatch`);
- neither that level nor the energy level may set `dispersion` or `solvation_method`
  (`enthalpy_atom_corrections_level_unverifiable`);
- the species must be heavy enough for the magnitude check to mean something. The raw
  total energy of a species made only of H, He and Li is estimated from its composition
  at 0.50, 2.90 and 7.43 hartree per atom. When that estimate is within ±2.0e4 kJ/mol
  (H15, He2, HeH and the Li atom are, for example), an uncorrected enthalpy cannot be told
  from a formation enthalpy and it is stripped
  (`enthalpy_formation_unverifiable_light_species`). A species with any heavier atom is
  not subject to this check. The composition is read from the record's xyz, else from its
  `formula`, which ARC 1.0 keeps for single atoms that it writes with `xyz: null`.

These checks are heuristics. They make an uncorrected or wrong-level enthalpy very unlikely
to pass; they do not prove that the corrections ran at the right level. The recorded switch
and level of output 1.2 replace them whenever present.

Two limits remain. Under `adaptive_levels`, output before 1.3 records one `sp_level` for
the run although species differ, so the level comparison can pass wrongly when
`restart.yml` is unavailable. For 1.2 species whose thermo Arkane loaded from its own YAML,
the header level describes this run and not the YAML's origin, so a foreign level cannot
be detected.

### G4 and G4MP2 on Gaussian 16 Revision A.03

ARC's energy for such a composite run is the shifted 298 K value. The record's sp energy,
the thermo content derived from E0 (H298, NASA, point H and G), the statmech energy links
and any kinetics built on it are withheld
(`g4_energy_loader_shifted_label_gaussian16_a03`). S298 and Cp are kept.

## Statmech

- `statmech_treatment` is `statmech.arkane_treatment` (output 1.3), the treatment Arkane
  applied. For earlier output a rotor-aware treatment is sent only when a freq Hessian was
  found for the species, because Arkane ignores every rotor without a force-constant
  matrix; otherwise it is omitted (`statmech_treatment_not_stated`).
- Torsions are sent with the treatment ARC records for each. ARC's rejected rotors are
  sent as torsions with `invalidated_reason`.
- The rigid-rotor kind is sent as stated.
- Statmech `electronic_levels` are not sent; ARC exports no electronic level data.

## Energy corrections

- Atom-energy, Petersson and Melius corrections become `applied_energy_corrections`, each
  with a scheme.
- A Petersson `bac_total` is sent with its bond components. Output 1.3 lists the bonds
  Arkane applied and the ones it skipped; the applied ones are sent as `components` and the
  skipped ones go in the correction's note. For earlier output, a total with no or partly
  null components is omitted (`bac_correction_omitted_components_incomplete`), because
  TCKDB refuses a total without components.
- `scheme.software` is the program named in the record's `matched_arkane_key`. It is
  omitted when the key names none, and omitted with
  `energy_correction_scheme_software_conflict` when it disagrees with the correction
  level's software.
- `scheme.data_revision` is the RMG-database revision Arkane read: the git commit, else
  the package version, else the SHA-256 of the data file. It is part of the scheme's
  identity. The Arkane build is `scheme.workflow_tool_release`, which is provenance only.
- An `atom_energy` scheme carries `atom_params` from ARC's `reference_atom_energies`, with
  the table's own unit and `atom_params_applied_as: subtracted`. A table with a missing or
  unknown unit is not sent.
- `frequency_level_of_theory` is sent only for a Petersson or Melius scheme whose Arkane
  key is a composite of a freq level and an energy level. It is omitted with
  `bac_frequency_level_conflict` when ARC's stated freq level disagrees with the key.
- Literature is not sent. ARC records no citation, and the adapter does not build one from
  a table name.

## Kinetics

- ARC's `A`, `n` and `Ea` become the modified Arrhenius fields, with `t0_k` equal to ARC's
  `T0_k` (omitted when it is 1 K, the contract default). `A` is sent as ARC states it. A
  `T0_k` that is null, invalid or outside 0 to 10000 K omits `a` and `a_units`.
- A value is never sent without a mapped unit. An unresolvable unit drops that one field
  and keeps the reaction.
- `dA` becomes a multiplicative uncertainty. `Tmin_k` and `Tmax_k` become the bounds.
- `tunneling` becomes `tunneling_model` as the contract's lowercase token (`eckart`;
  unknown methods `other`).
- The `note` carries Arkane's comment, ARC's `ts_validation` text and the kinetics run's
  atom-correction switch. `reversible` is sent as stated.
- Source calculations link the reactants', products' and transition state's energies, the
  TS freq and the IRC.

### Declarations for a fit Arkane produced

A kinetics record can declare its `direction`, `determination` and `applicability`. ARC
states none of these as fields. The adapter sends them only for a fit whose `tunneling` is
a string, which `output.yml` documents as stamped by ARC's Arkane kinetics run:

| Field | Sent | Basis |
|---|---|---|
| `direction` | `forward` | ARC writes the kinetics run on the reaction as reactants to products and has no reverse fit. |
| `determination` | whole reaction, complete, with a key built from the project, the TS label and a digest of the fit, the levels and the energies in `output.yml` | The `tunneling` stamp. Two different fits of the same energies get different keys. |
| `applicability` | rate coefficient, elementary coefficient, high-pressure limit, `claim_origin: depositor_interpretation` | ARC's kinetics job is its high-pressure-limit rate calculation. |
| `phase` | `gas`, only when no level states solvation | Arkane's statistical mechanics is gas phase. Every level is read: the built calculations', the header levels, the levels inside `adaptive_levels` and each participant's own `levels` entry. A level states solvation when it sets `solvent`, `solvation_method` or `solvation_scheme_level`, or when its free-form `args` contain `scrf`, `cpcm`, `smd`, `pcm`, `cosmo`, `solvent` or `solvation` in any letter case. |

These are the adapter's interpretation of ARC's documented provenance and not values read
from `output.yml`. The kinetics `note` therefore ends with a sentence that says so, and the
sidecar carries `kinetics_declarations_interpreted`. A fit without the stamp declares
nothing. The reaction order, the rate-progress convention and every `protocol` field are
not sent, because ARC states none of them.

## Transition states

- The TS record becomes the transition state of a reaction bundle, or a standalone upload
  in `computed_ts` mode. The standalone request carries rotor scans and applied energy
  corrections too.
- Modes are the frequencies in ESS order. The reaction-coordinate mode is the one ARC's
  normal-mode displacement check validated (`reaction_coordinate_mode_index`). When none is
  stated, TCKDB's 50 cm-1 noise floor picks the one mode above it. Two or more above it
  refuse the record.
- `reversible` on a standalone TS is the value the reaction states in output 1.3. When
  the reaction states none (earlier output, or a null), `true` is sent, because the route
  requires the field. This is one of the exceptions listed at the top.
- An `irc_result` is sent only when every contributing log's direction is stated.
  Otherwise the result is withheld (`irc_direction_not_stated`) and the `irc` calculation
  is still sent.

Validation evidence is built only from verdicts ARC states in `ts_checks`:

| ARC | Evidence | Notes |
|---|---|---|
| `ts_checks['IRC']` | `irc` | `passed` is ARC's verdict. `irc_converged` is never used; it means only that the IRC jobs finished. No verdict sends no evidence. |
| `ts_checks['freq']` | `imaginary_mode` | Count and frequency are read from the TS freq result in the same upload. `mode_displacement_agrees` is true only when ARC states the reaction-coordinate index, false for a failed or forced displacement check, and otherwise omitted. |
| `ts_checks['e_elect']` | `energy_ordering`, reaction bundle only | Each participant's `sp_energy_hartree` is cited to its sp calculation. A passing verdict that the stated energies contradict is not sent (`ts_energy_ordering_evidence_not_sent`). |

Atom maps:

- `ts_atom_map` (output 1.3) becomes TCKDB's `atom_map`, participant atom to TS atom, with
  `source: inferred` and a note naming ARC's method. It is sent after checks on block
  lengths, TS index coverage, element conservation and agreement with the IRC participant
  mapping. Otherwise no map is sent (`reaction_ts_atom_map_not_sent`).
- ARC's reactant-to-product `atom_map` is not sent. TCKDB's map is a different relation.
- `irc_participant_mapping` becomes the IRC evidence's participant mappings when ARC
  states that the IRC endpoints follow the TS atom order
  (`ts_irc_participant_mapping_not_sent` otherwise).
- `reactant_species_labels` and `product_species_labels` give the order and repeats of the
  reaction's participants. A contradiction with the other label lists refuses the reaction
  (`reaction_species_labels_contradicted`).

## Artifacts

With `artifacts.upload` on, a calculation's log (`output_log`) and input deck (`input`)
are sent. How depends on the upload mode:

| Mode | Artifacts |
|---|---|
| `computed_species`, `computed_reaction` | Encoded in base64 inside the bundle, under each calculation's `artifacts`. No separate request is made, and the standalone artifact call is suppressed in these modes. |
| `conformer` | Posted to `/calculations/{calculation_ref}/artifacts` after the conformer upload is accepted, one request per calculation, or several when its files total more than 64 MiB (the first under the plain key, the rest under `artifact-batch-2`, `-3`). |
| `computed_ts` | Not sent. The transition-state route has no artifact field and the sweep makes no artifact request in this mode. |

A file larger than `max_size_mb` is skipped in every mode. In `conformer` mode a response
that carries no `calculation_ref` leaves that calculation's artifacts unposted
(`calculation_ref_not_returned`), and each artifact has its own sidecar and an idempotency
key built from the calculation ref, the kind and the file's SHA-256.

## Not sent

| Data | Why |
|---|---|
| Transport properties | ARC exports none. |
| Dipole moment and polarizability | The routes this adapter uses have no field for them. |
| TS `statmech` on the reaction bundle | ARC's TS statmech record has not been mapped. |
| `tunneling_application`, `interpretation_assignments`, `network_kinetics_ref` | Each cites a record deposited earlier, and a bundle cannot cite what it is creating. |
| Typed level-of-theory qualifiers outside the table under "Levels of theory" | An unmapped token, an explicit negation, a program other than ORCA and Gaussian, a deck that cannot be read, a calculation ARC records no deck for (scans, path searches, the coarse opt stage, a placeholder opt) or a level that is no job's (scale-factor and correction-scheme levels, header levels). |
| Literature on correction schemes | ARC records no citation. |
| `scale_kind`, `electronic_state_kind` | ARC does not state them. |
| GSM `converged` and `is_climbing_image`; the geometries of NEB images other than the climbing image; the NEB-TS saddle point's energy and forces | The files ARC keeps do not hold them, or the contract has no field. See "Path-search points". |
| The `composite` calculation type | ARC does not export what it needs. |
| Rights | Sent only when the `tckdb` block has a `rights` block. No licence is inferred. |
