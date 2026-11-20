# tckdb-adapters-core

The part of a TCKDB upload adapter that does not depend on which tool produced the data. A
producer adapter (`tckdb_arc` today) reads its tool's output and builds TCKDB payloads.
This package takes over once a payload exists, and it also holds the helpers that every
producer would otherwise write the same way.

To write an adapter on top of it, read
[`docs/writing_an_adapter.md`](https://github.com/TCKDB/tckdb-adapters/blob/main/docs/writing_an_adapter.md).

## What belongs here

Anything that would be reused **verbatim** by another producer targeting the same
TCKDB contract, and **nothing that reads a producer's output format**.

| Module | What it holds |
|--------|---------------|
| `payload_writer` | Writes the payload JSON once and the sidecar eagerly; sidecar and replay types. |
| `uploader` | `TCKDBUploaderBase`: readiness probe with backoff, POST, sidecar bookkeeping (status, response, server warnings, request ids), strict mode, artifact batches. |
| `outcomes` | `UploadOutcome`, `ArtifactUploadOutcome`, `TCKDBReadinessError`. |
| `constants` | Route endpoints, payload kinds, the readiness retry policy, and the `origin_kind` values and log-file extensions read from tckdb-schemas. |
| `idempotency` | Idempotency-key composition. The key namespace is a required argument. |
| `config` | Config fields and defaults, API-key resolution (env, key file, dotenv file), the artifact kinds read from tckdb-schemas. No parser. |
| `level_rules` | Replica of TCKDB's level-of-theory identity (folded dispersion, hash), which tckdb-schemas does not ship. The method identity key is imported from `tckdb_schemas.method_names`. |
| `method_families` | `method_family` and `ONE_ELECTRON_EXEMPT_FAMILIES`, a thin surface over `tckdb_schemas.method_families`. |
| `constraints` | Held-fixed coordinate constraints and their serializer. |
| `keys` | Bundle-local key helpers (`safe_key_part`, `local_key_for_actor`, `calc_prefix_for_actor`). |
| `adapter_warnings` | `AdapterWarning` and `WarningSink`: the sidecar's warning dict shape, with the producer tag as a parameter. |
| `warning_codes` | `CodedEnum` (a code string plus a one-line description), `CoreWarning` (the codes this package emits) and `registry()`. A producer defines its own codes the same way. |
| `thermo_numerics` | `build_nasa_block`, `build_thermo_points`, `nasa_h298_kj_mol`: NASA-7 and Cp/H/S/G point shaping from neutral mappings. |
| `rmg_units` | RMG kinetics-unit strings to TCKDB's `ArrheniusAUnits` / `ActivationEnergyUnits` (`a_units_to_tckdb`, `ea_units_to_tckdb`). |
| `reaction_flatten` | `flatten_result_fields` / `flatten_all_reaction_calcs`: wrapped `opt_result`/`freq_result`/`sp_result` to the computed-reaction route's flat fields, with the completeness guard. |
| `composition` | Element symbols from an xyz string or a formula, a formula string, the single-atom test. |
| `xyz`, `physical_constants` | Element data, `xyz_from_data` / `check_xyz_dict` / `xyz_to_str`, and `E_h_kJmol` (ported from ARC; the one definition of the hartree-to-kJ/mol factor). |
| `isotopes` | The isotope rule for geometries (`species_geometry_isotope_mismatch`): substitutions, SMILES multisets, reconciliation error, the geometry block. |
| `route_level` | Route line vs level comparison (`route_method_basis`, `route_contradicts_level`). |
| `software_release` | `split_version_banner`: an ESS banner split into version, revision and build as TCKDB would. |
| `rules` | TCKDB rule pre-checks with the producer's facts as arguments: `energy_level_declaration` (`assert_role_consistency`), `kinetics_energy_level_assessment` (per-participant kinetics spin), `designate_reaction_coordinate_index` (the window rule), `designate_by_tau` (the tau rule), `describe_level`. |
| `ts_evidence` | Transition-state `validation_evidence` record shapes (`irc_record`, `energy_ordering_record`, `imaginary_mode_record`) and the numeric rule that a pass its own energies contradict is not sent. |
| `ts_evidence_rules` | `offline_evidence_errors`: an offline replica of the server's evidence rules, to run on any built request. |
| `testing` | The test kit (below). Needs `pytest` and `jsonschema`; never imported by the runtime package. |

## What does not belong here

- Reading a producer's files or keys. `tests/test_core_is_producer_agnostic.py` fails if a
  module under `tckdb_core` imports `arc` or `tckdb_arc`, or names `output.yml`,
  `parser_evidence`, `restart.yml` or `arc.`.
- Chemistry mappings that depend on how a producer states its results.
- Producer defaults. A producer sets `PRODUCER_TAG`, `PRODUCER_NAME` and
  `IDEMPOTENCY_NAMESPACE` on its `TCKDBUploaderBase` subclass. The core has no default for
  the tag or the namespace that would be right for anyone.
- Replicas of rules that `tckdb-schemas` ships. Import them. The replicas that remain
  (`level_rules`, parts of `rules`) cover rules that exist only in TCKDB's backend, and
  each has a parity test against it.

## Writing a producer

```python
from tckdb_core.adapter_warnings import WarningSink
from tckdb_core.constants import COMPUTED_SPECIES_ENDPOINT, COMPUTED_SPECIES_KIND
from tckdb_core.idempotency import IdempotencyInputs, build_idempotency_key
from tckdb_core.payload_writer import PayloadWriter
from tckdb_core.uploader import TCKDBUploaderBase
from tckdb_core.warning_codes import CodedEnum


class MyToolWarning(CodedEnum):
    REFERENCE_PRESSURE_NOT_STATED = (
        "thermo_reference_pressure_not_stated",
        "The source records no standard-state pressure, so reference_pressure_bar is omitted.",
    )


class MyToolAdapter(TCKDBUploaderBase):
    PRODUCER_TAG = "tckdb_mytool"       # sidecar warnings: source "tckdb_mytool_self_check"
    PRODUCER_NAME = "MyTool"
    IDEMPOTENCY_NAMESPACE = "mytool"    # keys: "mytool:<project>:..."
    LOGGER_NAME = "tckdb_mytool"

    def _build_species_payload(self, record, warnings):
        # The producer's own mapping. A real one builds the whole request and
        # reports each omission through a registered code.
        warnings.add(
            MyToolWarning.REFERENCE_PRESSURE_NOT_STATED,
            f"{record.label} states no standard-state pressure",
            "thermo.reference_pressure_bar",
            action="reference_pressure_omitted",
        )
        return {"species_entry": {"smiles": record.smiles}}

    def submit_species(self, record):
        if not self._config.enabled:
            return None
        warnings = WarningSink(self.PRODUCER_TAG)
        payload = self._build_species_payload(record, warnings)
        if self._config.rights is not None:
            payload["rights"] = dict(self._config.rights)
        key = build_idempotency_key(
            IdempotencyInputs.from_payload(
                project_label=self._config.project_label, species_label=record.label,
                conformer_label="bundle", payload_kind=COMPUTED_SPECIES_KIND, payload=payload),
            namespace=self.IDEMPOTENCY_NAMESPACE)
        written = self._writer.write(
            label=record.label, payload=payload, endpoint=COMPUTED_SPECIES_ENDPOINT,
            idempotency_key=key, payload_kind=COMPUTED_SPECIES_KIND,
            subdir=PayloadWriter.COMPUTED_SPECIES_SUBDIR,
            base_url=self._config.base_url, warnings=warnings)
        if not self._config.upload:
            return self._finalize_skipped(written)
        return self._upload(written, payload, endpoint=COMPUTED_SPECIES_ENDPOINT)
```

The order is fixed: build the payload, write it with its sidecar, then upload. The base
class owns everything after the write: the readiness probe with backoff, the POST, the
sidecar's status, response, warnings and request ids, strict mode, and artifact batches
(`_prepare_artifact_upload`, `_upload_artifact_batch`).

Hooks to override when needed: `_log` (the logger the pipeline writes to),
`_sleep_between_probes`, `_project_label_for(output_doc)` and `_log_readiness_recovery`.

### Logging

The logger belongs to the producer, so two producers can live in one process.
`TCKDBUploaderBase` logs through `self._log`, which is `logging.getLogger(cls.LOGGER_NAME)`
(`"tckdb_core"` by default). A producer sets `LOGGER_NAME`, or overrides the `_log`
property to return a logger object of its own. Free functions here that log
(`serialize_constraints`, `build_nasa_block`, `a_units_to_tckdb`, ...) take a keyword
`log=`; without one the record goes to the `tckdb_core` logger.

### Warning codes

Findings are emitted as `AdapterWarning(code, message, field, context)` or through
`WarningSink.add`, which renders the sidecar dict
`{code, message, field, context: {source: "<producer>_self_check", ...}}`. Codes are
members of a `CodedEnum` registry. The core's own are `CoreWarning`. A producer registers
its own enum and generates its code table from it; the ARC adapter's table is
`docs/contract/WARNING_CODES.md`.

## Test kit: `tckdb_core.testing`

Any adapter's tests can import it. It imports `pytest` and `jsonschema` at module level,
so it needs the `[test]` extra. `tckdb_core/__init__` does not import it, and the runtime
package works without those two.

| Module | Use |
|--------|-----|
| `contract` | `contract_validate(model, payload)`, `assert_request_is_accepted`, `REQUEST_MODELS`, `ROUTE_SCHEMAS`, and `TARGET_SCHEMAS_LINE`, the one tckdb-schemas line every suite pins. Validates with the published pydantic model, then the shipped JSON Schema. |
| `contract_hook` | `contract_checks_fixture(adapter_cls=..., payload_builders={"_build_x": "<schema name>", ...})`: the autouse fixture that checks every request the adapter builds. `register_markers(config)` registers `payload_refused_by_contract`. An adapter's `conftest.py` names its own builders. |
| `contract_pin` | The four assertions of a package's `test_contract_pin.py`: installed tckdb-schemas line, `pyproject.toml` bound, contract document stamp, a shipped schema for every route. |
| `stubs` | `StubClient`, `StubResponse`, `SequencedReadyzClient` and `upload_calls`: stand-ins for `TCKDBClient`, handed to an adapter through `client_factory`. |
| `backend_import` | `backend_modules()` / `backend_level_hash()`: import TCKDB's backend identity rules from `TCKDB_BACKEND_PATH`. `TCKDB_REQUIRE_BACKEND=1` turns "unavailable" into a failure. |
| `live` | The opt-in live gate: loopback and isolated-backend guards, retrying reads, settled row counts, the commit probe, and `live_tckdb_fixture()` for an integration `conftest.py` to register. |

## Public API

The modules in the table at the top are public, except names with a leading underscore.
Underscore-prefixed names are internals shared by the packages in this repository and can
change in any release. Helpers that used to carry an underscore and are now public
(`keys.safe_key_part`, `isotopes.xyz_symbols`, `outcomes.PreparedArtifactUpload`,
`uploader.artifact_batch_digest` and the others listed under 0.5.8 in the changelog) keep
their old names as aliases.

Import from `tckdb_core`, never through a producer package. `tckdb_arc` forwards the
public names of its old modules with a `DeprecationWarning`.

## Tests

```bash
pip install -e ".[test]"
TCKDB_BACKEND_PATH=/path/to/TCKDB/backend TCKDB_REQUIRE_BACKEND=1 python -m pytest -q
```

Without `TCKDB_BACKEND_PATH` the backend comparison of `level_rules` skips.

## Changes

See [CHANGELOG.md](https://github.com/TCKDB/tckdb-adapters/blob/main/tckdb_core/CHANGELOG.md).
