# tckdb-arc

The ARC adapter for TCKDB. It reads an ARC project's `output/output.yml`, with ARC's
parser evidence file when there is one, and builds TCKDB species, reaction and
transition-state uploads. Each payload is written to disk before any request is made, so
it can be inspected and replayed.

Requires Python 3.11+, `tckdb-client` 0.208.x and `tckdb-schemas` 0.175.x, and
`tckdb-adapters-core` from this repository. Install instructions are in the
[repository README](https://github.com/TCKDB/tckdb-adapters/blob/main/README.md).

## Use

ARC runs the adapter at the end of a run when the project's `input.yml` has an enabled
`tckdb` block. The same sweep can be run later on a finished project:

```bash
tckdb-arc-upload /path/to/project/input.yml --offline
tckdb-arc-upload /path/to/project/input.yml --upload-mode computed_reaction
tckdb-arc-upload /path/to/project/input.yml --project-directory /elsewhere/project
```

`--offline` writes payloads and sidecars and makes no request. `--upload-mode` overrides
the mode in the input file. The project directory is `--project-directory`, else
`project_directory` in the input file, else the directory that holds the input file.

From Python:

```python
from tckdb_arc import TCKDBAdapter, TCKDBConfig, run_upload_sweep

config = TCKDBConfig.from_dict(input_dict["tckdb"])        # None when the block is disabled
adapter = TCKDBAdapter(config, project_directory=project_directory)
run_upload_sweep(adapter=adapter, project_directory=project_directory, tckdb_config=config)
```

## The `tckdb` block

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Master switch. Nothing is built when it is off. |
| `base_url` | required | The TCKDB API root, for example `https://host/api/v1`. |
| `upload_mode` | `conformer` | One mode per run: `conformer`, `computed_species`, `computed_reaction` or `computed_ts`. |
| `upload` | `true` | `false` writes payloads and sidecars and sends nothing. |
| `payload_dir` | `tckdb_payloads` | Where payloads and sidecars go, relative to the project directory. |
| `api_key_env` | `TCKDB_API_KEY` | Name of the environment variable that holds the API key. |
| `api_key_file` | unset | A file that contains only the key. |
| `api_key_env_file` | unset | A dotenv-style file with a `<api_key_env>=` line (`TCKDB_API_KEY=` unless `api_key_env` is set). It is parsed, never executed. |
| `preflight` | `true` | Probe the server's `/readyz` before the first upload. |
| `validate_before_upload` | `true` | Check each request against the shipped tckdb-schemas contract (the published model for the conformer, computed-species, computed-reaction and transition-state routes, the JSON Schema for the rest) just before it is posted. A request that fails is not posted: the payload stays on disk, the sidecar is `failed` with `payload_failed_local_validation` and the findings, and `strict` raises. `false` posts whatever was built; use it when the installed tckdb-schemas is not the version the server runs. |
| `strict` | `false` | Raise on a failed upload. Otherwise the failure is logged and recorded in the sidecar, and the run continues. |
| `timeout_seconds` | `30` | Timeout of each phase of a request (connect, each read), not of the whole request. |
| `retry_max_attempts` | `3` | Attempts at one request, the first included, when it fails with a connection error, a timeout, or a 429, 502, 503 or 504. Each replay carries the same idempotency key, so TCKDB stores the upload once. A refusal is not replayed. `1` turns retrying off. An integer from 1 to 10. |
| `retry_backoff_seconds` | `1.0` | Each wait is jittered and capped at 30 s: the first is between 0.5 and 1 times this, and the un-jittered wait doubles each time (each wait is jittered separately). A `Retry-After` from the server is honoured, up to 120 s per wait. A number of at least 0. |
| `project_label` | the project name in `output.yml` | Part of every idempotency key. |
| `artifacts` | off | `{upload: true, kinds: [output_log, input], max_size_mb: 50}` sends calculation logs and input decks. In `computed_species` and `computed_reaction` mode they travel inside the bundle; in `conformer` mode they are posted separately after each upload; `computed_ts` mode sends none. Files over `max_size_mb` are skipped. |
| `allow_partial_uploads` | `true` | Write a `.partial` sidecar for a reaction whose transition state did not converge. A partial sidecar is never posted. |
| `rights` | unset | The upload's `rights` fragment (licence, the depositor's attestation, optional source terms), validated against `tckdb_schemas.rights.DepositRights`. Without it no rights are sent. |

The key is never stored in YAML. The environment variable wins over either file.

Retry notes. `timeout_seconds` applies to each phase of a request (connect, each read), not to the
whole request. A request that makes no progress is given up after `timeout_seconds`, so with the
defaults one upload can then take about three such timeouts plus the waits; a slow but progressing
large upload can run longer on each attempt. Each wait is jittered separately and capped at 30 s:
the first is between 0.5 and 1 times `retry_backoff_seconds`, and the un-jittered wait doubles each
time. When the server sends `Retry-After` the client waits at least that long, up to 120 s per wait,
and does not replay at all above 120 s. The readiness probe runs once per uploader, so a server that
degrades mid-run costs this on every remaining upload. TCKDB does not lock an idempotency key while
a request is in progress. After a client-side timeout both the original request and the replay can
run, and whichever commits second is refused with `409 idempotency_conflict` and rolled back, so no
duplicate record is created. Either the original or the replay can be the one refused, so the
sidecar can be `failed` with `server_error_code: idempotency_conflict` although the data was stored.
Running the upload again later returns the stored response when the server stored a receipt for the
write; a response that carries `Idempotency-Receipt: failed` means it did not, and a later run
executes the write again.

Boolean keys (`enabled`, `upload`, `preflight`, `strict`, `validate_before_upload`, `allow_partial_uploads`) are read with Python's `bool()`: write YAML `true` or `false` (unquoted). A quoted `"false"` or `"no"` is a non-empty string and counts as true.

The upload modes map to TCKDB routes:

| Mode | Route | One request per |
|---|---|---|
| `conformer` | `POST /uploads/conformers` | converged species |
| `computed_species` | `POST /uploads/computed-species` | converged species, with its thermo, statmech and corrections |
| `computed_reaction` | `POST /uploads/computed-reaction` | reaction, with its species, transition state and kinetics |
| `computed_ts` | `POST /uploads/transition-states` | converged transition state |

## What is written

For each upload the adapter writes `<label>.payload.json` (the exact request body) and
`<label>.meta.json` (the sidecar) in a subdirectory of `payload_dir` named after the upload
kind. A later run that builds a different payload for the same label moves the earlier pair
into `archive/` first. The sidecar starts as
`pending` and ends as `uploaded`, `skipped` or `failed`. It holds the endpoint, the
idempotency key, the response, the server's warnings and the adapter's own.

An idempotency key is `arc:<project>:<species>:<conformer>:<kind>:<payload hash>`. The
same ARC output gives the same key, and TCKDB answers a repeated key by replaying the first
response. A release of this adapter that changes a payload changes its hash, and TCKDB
then records a re-upload as a new deposit. The [changelog](https://github.com/TCKDB/tckdb-adapters/blob/main/tckdb_arc/CHANGELOG.md) marks those
releases.

The adapter's warnings each carry a stable code, and
[`docs/contract/WARNING_CODES.md`](https://github.com/TCKDB/tckdb-adapters/blob/main/docs/contract/WARNING_CODES.md) lists them all.

## Evidence files and the optional ARC import

Hessians, IRC trajectories and GSM paths come from ARC's `parser_evidence.json`, which
ARC writes beside `output.yml`. The older `tckdb_evidence.json` is still read. With either
file the adapter needs neither ARC nor the calculation logs.

Without an evidence file, those three kinds of data can be parsed again from the logs
through `arc.parser`, which needs ARC importable. ARC is not installable with pip, so the
`[arc]` extra is an empty marker and declares nothing. When ARC is absent the three
sub-payloads are left out and the rest of the upload is unchanged.

## What is sent

[`docs/contract/ARC_MAPPING.md`](https://github.com/TCKDB/tckdb-adapters/blob/main/docs/contract/ARC_MAPPING.md) is the reference: which
ARC fields become which TCKDB fields, the checks an enthalpy must pass before it is
declared a formation enthalpy, and what the adapter leaves out because ARC does not state
it. Two rules cover most of it:

- A value ARC does not state is omitted and reported with a warning code.
- When the adapter sends its own reading of ARC's documented behaviour, the payload and
  the sidecar both say that it is an interpretation.

Three documented exceptions exist: the formation-enthalpy declaration for output before
1.2, `reversible: true` on a standalone transition state whose reaction states none, and
the opt level on an IRC calculation whose level is not stated. The mapping page lists
them under "Exceptions to rules 1 and 3".

## Module layout

The adapter's code is in `tckdb_arc/`. `adapter.py` holds the `TCKDBAdapter` class, the tables only
the class reads, and the re-exports. The rest is in sibling modules, each importing only the ones
listed before it, and `tckdb_arc.adapter` still exports every name that moved.

- `self_check.py` renders the sidecar warning dict of one of the adapter's own findings.
- `geometry.py` normalises xyz text, checks isotope lists and builds the geometry block.
- `levels.py` resolves the level a calculation ran at, compares levels, and reads ARC's adaptive and restart markers.
- `calculations.py` holds the calculation keys, the small result-payload builders and the readers of a calculation's route, reuse and final settings.
- `path_search.py` resolves the log field of a transition-state guess, reads xTB and GSM node outputs, and holds the IRC level, direction and zero-energy reference.
- `thermo.py` holds the checks that decide whether an enthalpy is a formation enthalpy, the applied-energy-correction helpers and the composite-energy checks.
- `provenance.py` reads the output-schema version, indexes species and transition states, and reads software releases and data revisions.
- `statmech.py` classifies rotor and mode treatments and lists the calculations a statmech block cites.
- `kinetics.py` holds the kinetics determination key, the stated interpretation of an Arkane-stamped fit, tunneling labels and electron counts.
- `reactions.py` holds reaction participant slots, the transition-state atom-map slots and the transition-state SMILES handle.
- `core_aliases.py` binds the core's unit maps, reaction-flattening tables and rule helpers under the adapter's names, and wraps the unit, NASA and thermo-point builders so they log through the package logger.
- `level_payloads.py` turns ARC's level into a TCKDB level reference and builds the energy-level declaration and the levels a conformer states.
- `calculation_payloads.py` builds the frequency, single-point and monatomic result payloads, serialises constraints, and anchors Hessian and point geometries.
- `thermo_payloads.py` builds the applied energy corrections and the thermo block, and withholds energies a G4 loader shifted.
- `statmech_payloads.py` builds the statmech block, its torsions and rejected torsions, the frequency scale factor reference and the scan entries.
- `kinetics_payloads.py` builds the kinetics block and declares the level of its source energies.
- `path_payloads.py` builds the IRC and path-search result payloads.
- `ts_evidence_payloads.py` builds the IRC, energy-ordering and imaginary-mode evidence of a transition state.
- `reaction_payloads.py` builds the stated participants of a reaction and its atom map.
- `finding_warnings.py` holds the `_warn_*` functions that log a finding and return its sidecar warning.
- `adaptive.py`, `arc13.py`, `evidence.py`, `constraints.py`, `orca_neb.py`, `config.py`, `sweep.py`, `cli.py` and `warning_codes.py` are the modules that were already separate.

Every module logs through the one `tckdb_arc` logger object in `_logging.py`, so tests that capture
log records patch the methods of that logger (`mock.patch.object(tckdb_arc._logging.logger, "warning")`).
A test that patches another name on a module of the adapter patches it on the module whose code looks
it up; `tests/test_adapter_module_surface.py` lists those names and checks the list against the tests.

## Tests

```bash
pip install -e "../tckdb_core[test]" -e ".[test]"
python -m pytest
```

Tests that import ARC skip when ARC is not on the path. See the repository README for the
backend comparison tests and the live integration gate.

## Changes

See [CHANGELOG.md](https://github.com/TCKDB/tckdb-adapters/blob/main/tckdb_arc/CHANGELOG.md).
